"""Schedule runs off the daemon's tick thread (Daemon.run_schedules).

A command, heal or builtin watcher schedule runs in a thread of its own, with its own database
connection, so a slow or hung one (a gh call, ssh to a box that does not answer) never holds the tick:
messages, dispatch and the other steps go on. The tick starts due runs, records the ones that ended
with the usual failure handling, and never starts a schedule whose run is still going. A run past its
timeout has its process groups killed and is recorded as a failed run.

The process groups a run starts (spawned) are kept in the database while they live, so a daemon that
stops or restarts kills the ones its runs left (kill_all); such a run recorded nothing, so its schedule
is still due and runs once more. A run's result is recorded only when it ends, never twice.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from .runner import proc_start

GROUPS_KEY = "schedule_groups"   # kv: pgid -> {schedule, start}: live process groups of schedule runs

_local = threading.local()


class Run:
    """One schedule run in its thread. `result` is (status, run again at) once `done` is set."""

    def __init__(self, s: dict, payload: dict, timeout: float):
        self.s, self.payload, self.timeout = s, payload, timeout
        self.name = s["name"]
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.done = threading.Event()
        self.result: tuple[str, float | None] = ("error: run ended without a result", None)
        self.timed_out = False   # recorded failed at its timeout; its late result is dropped
        self.groups: dict[int, str | None] = {}   # pgid -> its leader's start token
        self.db: Any = None


def current() -> Run | None:
    """The schedule run on this thread, if any."""
    return getattr(_local, "run", None)


def start(run: Run, body: Callable[[], tuple[str, float | None]], open_db: Callable[[], Any]) -> Run:
    """Run `body` in a daemon thread with its own database connection (`open_db`)."""
    def work() -> None:
        _local.run = run
        try:
            run.db = open_db()
            run.result = body()
        except Exception as e:
            run.result = (f"error: {type(e).__name__}: {e}"[:200], None)
        finally:
            _local.run = None
            try:
                if run.db is not None:
                    run.db.close()
            except Exception:
                pass
            run.done.set()
    threading.Thread(target=work, daemon=True, name=f"schedule:{run.name}").start()
    return run


def spawned(pid: int) -> None:
    """The schedule run on this thread started process group `pid` (a session leader)."""
    run = current()
    if run is None or run.db is None:
        return
    start_token = proc_start(pid)
    run.groups[pid] = start_token
    with run.db.tx():
        groups = run.db.kv(GROUPS_KEY, {}) or {}
        groups[str(pid)] = {"schedule": run.name, "start": start_token}
        run.db.set_kv(GROUPS_KEY, groups)


def ended(pid: int) -> None:
    """Process group `pid` of the schedule run on this thread is over."""
    run = current()
    if run is None or run.db is None:
        return
    run.groups.pop(pid, None)
    _forget(run.db, [pid])


def _forget(db: Any, pids: list[int]) -> None:
    with db.tx():
        groups = db.kv(GROUPS_KEY, {}) or {}
        if any(str(p) in groups for p in pids):
            db.set_kv(GROUPS_KEY, {k: v for k, v in groups.items() if int(k) not in pids})


def _kill(pid: int, start_token: str | None) -> bool:
    """SIGKILL group `pid`, unless its pid now belongs to another process (a new start token).
    A leader that is gone leaves the group's other members, which no other group can share the id of."""
    now = proc_start(pid)
    if now is not None and start_token is not None and now != start_token:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def kill(db: Any, run: Run) -> int:
    """Kill the process groups of `run` (past its timeout); returns how many were signalled."""
    groups = dict(run.groups)
    n = sum(_kill(pid, tok) for pid, tok in groups.items())
    _forget(db, list(groups))
    return n


def kill_all(db: Any) -> list[str]:
    """Kill every recorded group: at a daemon's start (its predecessor's runs died with it) and at its
    stop. Returns one line per group killed, for the log."""
    groups = db.kv(GROUPS_KEY, {}) or {}
    lines = [f"killed process group {pid} left by schedule {rec.get('schedule')}"
             for pid, rec in groups.items() if _kill(int(pid), rec.get("start"))]
    if groups:
        _forget(db, [int(p) for p in groups])
    return lines


def run_group(cmd: str, *, cwd: str, timeout: float, env: dict | None) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, shell=True) in a session of its own, kept in GROUPS_KEY while it runs. A
    timeout kills the whole group (the shell's children too) and raises TimeoutExpired."""
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cwd, env=env, start_new_session=True)
    spawned(proc.pid)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:   # a child that left the group still holds the pipes
                pass
            raise
    finally:
        ended(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
