# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What detached and looping drivers report: `ttp progress report <kind> --key <key> "<reason>"`.

A filler, recheck loop or guard that a worker or schedule started reports each counted result,
refusal, stall and failure here, one line from bash. Each report is one JSON line in the project's
state/progress.jsonl: {"ts", "key", "kind", "reason", "source"} plus the reporting driver's pid
(the caller's parent), boot id and, inside a `ttp detach` job, the job's .rc path, task and run.

The file is append-only under a lock (progress.lock) and synced per line, so concurrent reports
never interleave. It needs no daemon: a report made while the daemon is down is read once it is
back. It is bounded: past MAX_BYTES it rotates to progress.1.jsonl (one old generation is kept).

Readers: `streaks` is the model-free reading every consumer shares: per key, the reports since its
last result. N refusals, stalls or failures in a row with no result (waiting.progress.stall_after,
default 5) are a stalled key. The daemon raises one keyed observation per streak
(Daemon.watch_progress); `ttp status` and the web app list a driver whose process still runs as
stalled, not running (`stalled`).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

from .project import durable_append

LOG, OLD, LOCK = "progress.jsonl", "progress.1.jsonl", "progress.lock"
MAX_BYTES = 256 * 1024
REASON_MAX = 500
KINDS = ("result", "refusal", "stall", "failure")
BAD = frozenset(KINDS) - {"result"}   # count toward a stall; a result ends one
JOB_ENV = "TTP_DETACH_RC"   # set by `ttp detach` in its job's environment: the job's .rc path
DEFAULTS = {"stall_after": 5, "window_s": 86400}


def settings(cfg: dict | None) -> dict:
    got = ((cfg or {}).get("waiting") or {}).get("progress") or {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS and v is not None}}


def _one_line(text: str, cap: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:cap]


def record(kind: str, key: str, reason: str, env: dict | None = None, now: float | None = None) -> dict:
    """The line a report writes, from the caller's environment."""
    from .runner import boot_id
    env = os.environ if env is None else env
    job = env.get(JOB_ENV) or None
    task, run = env.get("TTP_TASK") or None, env.get("TTP_RUN_ID") or None
    pid = os.getppid()
    who = [f"task {task}" if task else "", f"run {run}" if run else "",
           f"job {Path(job).stem}" if job else "", f"pid {pid}"]
    return {"ts": round(time.time() if now is None else now, 3), "key": _one_line(key, 200), "kind": kind,
            "reason": _one_line(reason, REASON_MAX), "source": " ".join(w for w in who if w),
            "pid": pid, "boot": boot_id(), "job": job, "task": task, "run": run}


def append(state: Path, rec: dict, max_bytes: int = MAX_BYTES) -> None:
    """Append one report under the lock, rotating first when it would pass max_bytes. Raises OSError
    when the state folder cannot be written (the caller says so and still exits 0)."""
    state = Path(state)
    line = json.dumps(rec, sort_keys=True) + "\n"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / LOCK, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        log = state / LOG
        try:
            size = log.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(line) > max_bytes:
            os.replace(log, state / OLD)
        durable_append(log, line)


def entries(state: Path) -> list[dict]:
    """Every report kept, oldest first; unreadable lines are skipped."""
    out = []
    for name in (OLD, LOG):
        try:
            text = (Path(state) / name).read_text(errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("key") and rec.get("kind") in KINDS:
                out.append(rec)
    return out


def streaks(recs: list[dict]) -> dict[str, dict]:
    """Per key, its reports since its last result: {"bad": how many, "first"/"last": the streak's
    first and last report (None while bad is 0), "kinds": count per kind, "result": the last result}."""
    out: dict[str, dict] = {}
    for r in sorted(recs, key=lambda x: float(x.get("ts") or 0)):
        s = out.setdefault(r["key"], {"key": r["key"], "bad": 0, "first": None, "last": None, "kinds": {},
                                      "result": None})
        if r["kind"] == "result":
            s.update(bad=0, first=None, last=None, kinds={}, result=r)
            continue
        s["bad"] += 1
        s["first"] = s["first"] or r
        s["last"] = r
        s["kinds"][r["kind"]] = s["kinds"].get(r["kind"], 0) + 1
    return out


def stalled_keys(recs: list[dict], n: int, window_s: float, now: float) -> list[dict]:
    """The keys whose last n or more reports are refusals, stalls or failures with no result, the
    latest of them within window_s."""
    return [s for s in streaks(recs).values()
            if s["bad"] >= max(1, int(n)) and now - float(s["last"].get("ts") or 0) <= window_s]


def _alive(r: dict) -> bool:
    """Whether the driver that made report r still runs: its detached job (detach.state), else its pid
    on this boot."""
    from . import detach
    from .runner import same_boot
    if r.get("job"):
        try:
            return detach.state(Path(r["job"]))[0] == "running"
        except OSError:
            return False
    try:
        pid = int(r.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or not same_boot(r.get("boot")):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe(s: dict) -> str:
    """'4 refusals, 1 failure, no result since <first report>: <last reason>' without the time."""
    kinds = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(s["kinds"].items(), key=lambda kv: (-kv[1], kv[0])))
    return f"{kinds} in a row, no result: {s['last'].get('reason') or '(no reason given)'}"


def stalled(state: Path, cfg: dict | None, now: float | None = None) -> list[dict]:
    """Drivers whose process still runs but whose last stall_after reports are refusals, stalls or
    failures with no result: {"key", "source", "bad", "since", "line"}, the longest streak first."""
    s = settings(cfg)
    now = time.time() if now is None else now
    out = []
    for k in stalled_keys(entries(state), s["stall_after"], float(s["window_s"]), now):
        if not _alive(k["last"]):
            continue
        out.append({"key": k["key"], "source": k["last"].get("source") or "", "bad": k["bad"],
                    "since": float(k["first"].get("ts") or 0), "line": describe(k)})
    return sorted(out, key=lambda x: -x["bad"])
