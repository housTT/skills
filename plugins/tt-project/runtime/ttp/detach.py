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

Each request carries an id, which <name>.queued names too. The queue folder's .lock serializes
publishing a request (enqueue), settling one (_settle) and telling whether one is pending (pending):
a newer request that arrives while the daemon starts an older one is never deleted with it, and a
marker whose request is gone (a crash between the two writes) is cleared instead of hiding the result.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
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
    """Queue the job for the daemon to start, replacing a request of the same job not started yet;
    returns the queue file. The request and its marker are published together, under the lock."""
    rc = Path(rc)
    q = Path(state) / QUEUE / f"{rc.parent.name}-{rc.stem}.json"
    keep = {k: v for k, v in env.items() if k not in _SKIP_ENV and not _SECRET_ENV.search(k)}
    rid = uuid.uuid4().hex
    with _locked(q.parent):
        write_json(q, {"rc": str(rc), "log": str(log), "command": cmd, "cwd": cwd, "env": keep, "id": rid,
                       "queued": time.time(), **({"checks": checks} if checks is not None else {})}, mode=0o600)
        durable_write(queued(rc), f"{q}\n{rid}\n")
    return q


_held = threading.local()


@contextmanager
def _locked(folder: Path):
    """Hold the queue folder's lock (briefly: never while a job starts). Re-entrant within a thread,
    so a nested take never deadlocks on the thread's own lock."""
    key = str(folder)
    held = _held.__dict__.setdefault("folders", set())
    if key in held:
        yield
        return
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def _marker(rc: Path) -> tuple[Path | None, str | None]:
    """The queue file and request id <name>.queued names; (None, None) without one."""
    try:
        lines = queued(rc).read_text().split("\n")
    except OSError:
        return None, None
    return (Path(lines[0].strip()) if lines[0].strip() else None), ((lines[1].strip() if len(lines) > 1 else "") or None)


def _request_id(q: Path) -> str | None:
    try:
        m = json.loads(q.read_text())
    except (OSError, ValueError):
        return None
    return m.get("id") if isinstance(m, dict) else None


def pending(rc: Path) -> bool:
    """Whether a queued request of this job waits for the daemon (or is being started). A marker
    whose queue file is gone is an orphan (its request was consumed or never published): it is
    removed, so the job's own result answers again."""
    rc = Path(rc)
    q, _ = _marker(rc)
    if q is None:
        queued(rc).unlink(missing_ok=True)   # none, or one naming no request
        return False
    try:
        with _locked(q.parent):
            q, _ = _marker(rc)
            if q is None:
                return False
            if q.exists():
                return True
            queued(rc).unlink(missing_ok=True)
            return False
    except OSError:
        return q.exists()


def withdraw(rc: Path) -> None:
    """Drop the job's pending request (a foreground launch replaces it)."""
    q, _ = _marker(rc)
    if q is None:
        queued(rc).unlink(missing_ok=True)
        return
    with _locked(q.parent):
        q.unlink(missing_ok=True)
        queued(rc).unlink(missing_ok=True)


def _settle(q: Path, rc: Path, rid: str | None, lost_why: str = "") -> None:
    """Remove the request the daemon consumed, and its marker, only while they still name it: a
    newer request queued meanwhile stays for the next tick. `lost_why` records why it did not start,
    again only for a request not replaced."""
    with _locked(q.parent):
        if q.exists() and _request_id(q) != rid:
            return   # replaced: the newer request and its marker stay
        if lost_why:
            _mark_lost(rc, lost_why)
        q.unlink(missing_ok=True)
        if _marker(rc)[1] == rid:
            queued(rc).unlink(missing_ok=True)


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
        rid, why = m.get("id"), ""
        if time.time() - float(m.get("queued") or 0) > QUEUED_S:
            why = (f"the daemon did not start it within {QUEUED_S // 60} min of it being queued "
                   f"(was the daemon down?)")
        elif "checks" in m:
            # Replacement checks must cancel their earlier host pid here, never in the tool's
            # PID namespace. An earlier checks.rc is removed only after that process ends. The
            # request stays queued while they start, so a crash here starts them again; checks.pid
            # naming this request means they already started (a restart after the launch).
            from argparse import Namespace
            from .cli import _checks_start, _read_checks_pid
            if not rid or _read_checks_pid(rc.parent).get("req") != rid:
                try:
                    c = m["checks"]
                    lost(rc).unlink(missing_ok=True)
                    pid = _checks_start(Namespace(cmd=c["cmd"], fresh=c["fresh"]), rc.parent, c["worktree"],
                                        c["head"], {**os.environ, **(m.get("env") or {})}, rid or "")
                    renice(pid, nice_level(p.config().get("runner"))[0])
                except (OSError, ValueError, TypeError, KeyError) as e:
                    why = f"the daemon could not start the checks: {e}"
        elif not (lost(rc).exists() or rc.exists()):
            lock = locks.try_take([locks.job_lock(rc)], f"detached job {rc.stem}", " ".join(m["command"]))
            if lock is not None:   # else it already runs
                try:
                    pid = start(rc, log, list(m["command"]), m["cwd"], {**os.environ, **(m.get("env") or {})}, lock)
                    renice(pid, nice_level(p.config().get("runner"))[0])
                except (OSError, ValueError, TypeError) as e:   # its folder went, say: never retried each tick
                    why = f"the daemon could not start it: {e}"
                finally:
                    lock.close()
        _settle(q, rc, rid, why)


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
        if pending(rc):
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
