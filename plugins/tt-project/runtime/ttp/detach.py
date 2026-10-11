# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Local `ttp detach` jobs (cli.cmd_detach): start them, queue them for the daemon, tell their state.

A job writes <name>.log, then <name>.rc with its exit code, in the run's folder. Its wrapper writes
<name>.started the moment it runs and holds <name>.lock (locks.job_lock) while any of its processes
lives, so a job gone without an .rc is told apart from one still running.

A sandbox that runs each command in a PID namespace or a cgroup of its own can end every process the
command started once it returns, a new session or not: the job would die before its first line. As
for `ttp push --detach`, a launcher that finds itself boxed (push._boxed) queues the job under
state/detach-queue, with <name>.queued next to its .rc, and the daemon starts it outside the sandbox
(tend). A job gone without an exit code gets <name>.lost holding why, so its probe and the next run
read the reason, never a bare "gone".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import locks
from .project import Project, durable_write, nice_level, renice, write_json

QUEUE = "detach-queue"   # under the project's state: <run>-<name>.json per job a sandboxed command queued
QUEUED_S = 900           # a queued job the daemon has not started by then is recorded as lost
# Environment a queued job does not take from the run that queued it: the run's sandbox markers, and
# secrets, which stay off disk (the daemon's own environment supplies them).
_SKIP_ENV = ("TTP_PIDNS", "TTP_CGROUP")
_SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|_KEY$|AUTH", re.I)
WRAPPER = ('o="$1" r="$2" s="$3"; shift 3; echo $$ >"$s"; "$@" >"$o" 2>&1 </dev/null; c=$?; '
           'echo $c >"$r.tmp"; mv "$r.tmp" "$r"')


def started(rc: Path) -> Path:
    return Path(rc).with_suffix(".started")


def queued(rc: Path) -> Path:
    return Path(rc).with_suffix(".queued")


def lost(rc: Path) -> Path:
    return Path(rc).with_suffix(".lost")


def start(rc: Path, log: Path, cmd: list, cwd: str, env: dict, lock) -> int:
    """Start the job's wrapper in a session of its own, holding `lock` (its job lock, which the
    caller took and closes after). Returns its pid."""
    proc = subprocess.Popen(["/bin/sh", "-c", WRAPPER, "sh", str(log), str(rc), str(started(rc)), *cmd],
                            cwd=cwd, env={**env, "TTP_DETACHED": "1"}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            pass_fds=(lock.fileno(),))
    return proc.pid


def enqueue(state: Path, rc: Path, log: Path, cmd: list, cwd: str, env: dict, *, checks: dict | None = None) -> Path:
    """Queue the job for the daemon to start; returns the queue file."""
    rc = Path(rc)
    q = Path(state) / QUEUE / f"{rc.parent.name}-{rc.stem}.json"
    keep = {k: v for k, v in env.items() if k not in _SKIP_ENV and not _SECRET_ENV.search(k)}
    write_json(q, {"rc": str(rc), "log": str(log), "command": cmd, "cwd": cwd, "env": keep,
                   "queued": time.time(), **({"checks": checks} if checks is not None else {})}, mode=0o600)
    durable_write(queued(rc), str(q) + "\n")
    return q


def _mark_lost(rc: Path, why: str) -> None:
    try:
        durable_write(lost(rc), why + "\n")
    except OSError:
        pass


def tend(p: Project) -> None:
    """The daemon's part, each tick: start the queued jobs, and record as lost those queued too long
    ago or that cannot start. The .lost goes before the queue entry, so a probe never sees neither."""
    folder = p.state / QUEUE
    for q in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            m = json.loads(q.read_text())
            rc, log = Path(m["rc"]), Path(m["log"])
        except (OSError, ValueError, KeyError, TypeError):
            q.unlink(missing_ok=True)
            continue
        if time.time() - float(m.get("queued") or 0) > QUEUED_S:
            _mark_lost(rc, f"the daemon did not start it within {QUEUED_S // 60} min of it being queued "
                           f"(was the daemon down?)")
        elif "checks" in m and not lost(rc).exists():
            # Replacement checks must cancel their earlier host pid here, never in the tool's
            # PID namespace. An earlier checks.rc is removed only after that process ends.
            from argparse import Namespace
            from .cli import _checks_start
            try:
                c = m["checks"]
                pid = _checks_start(Namespace(cmd=c["cmd"], fresh=c["fresh"]), rc.parent,
                                    c["worktree"], c["head"], {**os.environ, **(m.get("env") or {})})
                renice(pid, nice_level(p.config().get("runner"))[0])
            except (OSError, ValueError, TypeError, KeyError) as e:
                _mark_lost(rc, f"the daemon could not start the checks: {e}")
        elif not (lost(rc).exists() or rc.exists()):
            lock = locks.try_take([locks.job_lock(rc)], f"detached job {rc.stem}", " ".join(m["command"]))
            if lock is not None:   # else it already runs
                try:
                    pid = start(rc, log, list(m["command"]), m["cwd"], {**os.environ, **(m.get("env") or {})}, lock)
                    renice(pid, nice_level(p.config().get("runner"))[0])
                except (OSError, ValueError, TypeError) as e:   # its folder went, say: never retried each tick
                    _mark_lost(rc, f"the daemon could not start it: {e}")
                finally:
                    lock.close()
        queued(rc).unlink(missing_ok=True)
        q.unlink(missing_ok=True)


def _entry(rc: Path) -> dict:
    try:
        jobs = json.loads((rc.parent / "detached.json").read_text())
    except (OSError, ValueError):
        return {}
    return next((j for j in jobs if isinstance(j, dict) and j.get("rc") == str(rc)), {}) \
        if isinstance(jobs, list) else {}


def state(rc: Path) -> tuple[str, str]:
    """("ended", exit code), ("running" or "queued", ""), or ("lost", why). The first look at a lost
    job records why in <name>.lost."""
    rc = Path(rc)
    for _ in range(2):   # a job that ends between the looks wrote its .rc before its lock went free
        if rc.exists():
            return "ended", rc.read_text().strip() or "?"
        if lost(rc).exists():
            return "lost", lost(rc).read_text().strip() or "no reason recorded"
        if queued(rc).exists():
            return "queued", ""
        if not locks.any_free([locks.job_lock(rc)]):
            return "running", ""
    from .runner import same_boot
    boot = _entry(rc).get("boot")
    if boot and not same_boot(boot):
        why = "the host restarted while it ran"
    elif not started(rc).exists():
        why = ("it never started: it was killed with the command that ran `ttp detach` (a sandbox or tool "
               "that ends the processes a command starts)")
    else:
        why = "it was killed after it started, before it wrote an exit code"
    _mark_lost(rc, why)
    return "lost", why
