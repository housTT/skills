# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A host-wide cap on how many `ttp checks` runs execute their commands at once.

Many tasks of many projects may each start a full test suite at the same time; together they can
load the host so far that load-sensitive tests time out and every run fails. Each `ttp checks` that
has commands to run first takes one of `limit()` slots under ~/.tt-project/check-slots, shared by
every project of this account on this host. A run over the cap waits its turn in arrival order
(locks.enqueue), sleeping between tries: no failure, no model run.

Slots and queue tickets are OS file locks (locks.py), so they end with the process that holds them:
a crash, a kill or a reboot never leaves a stale holder.

The cap is the account setting `checks.host_max` (`ttp config --account checks.host_max N`), or the
environment's TTP_CHECKS_HOST_MAX; 0 turns it off. Unset, it is one slot per CORES_PER_SLOT cores,
at least one. Check commands run with HELD_ENV set, so a `ttp checks` nested in them runs at once
instead of waiting for the slot its parent holds.

Push checks (the push queue's batches and `ttp push` to the push branch) hold the push lock, so every
approved change waits on them: they never queue behind task checks. Each push branch has one priority
slot of its own beyond the cap (`acquire_priority`), keyed by the remote and branch so it is one per
branch host-wide, across projects. Task checks keep their cap and their order: the priority slot never
takes one of theirs. It is an OS file lock like the others, so a killed push frees it too.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable

from . import locks, poll_s, project

RESOURCE = "checks"
PRIORITY = "checks-push"   # check-slots/checks-push.<key>.lock: one priority slot per push branch
CORES_PER_SLOT = 16
HELD_ENV = "TTP_CHECKS_SLOT"
POLL_S = 5.0
TELL_S = 300.0
# A `ttp checks` in a tool call waits this long for a slot at most, then hands over to detached checks
# (cli.cmd_checks): a full queue can take many minutes, longer than a tool call may block.
FOREGROUND_WAIT_S = 60.0
# The arrival time a foreground run hands to its detached checks, so they keep its place in the queue.
ARRIVED_ENV = "TTP_CHECKS_ARRIVED"


def slot_dir() -> Path:
    return project.HOME_DIR / "check-slots"


def cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def default_limit() -> int:
    return max(1, cores() // CORES_PER_SLOT)


def _number(v) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def limit() -> int:
    """Slots on this host: TTP_CHECKS_HOST_MAX, else the account's checks.host_max, else the
    default from the core count. 0 means no cap; an unreadable value is the default."""
    for v in (os.environ.get("TTP_CHECKS_HOST_MAX"), (project.load_account_settings().get("checks") or {}).get("host_max")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return default_limit()


def label(project_name: str | None, what: str = "") -> str:
    """Who holds or waits for a slot, as status lines show it: the project and the task's run."""
    task, run = os.environ.get("TTP_TASK"), os.environ.get("TTP_RUN_ID")
    who = f"{project_name or 'no project'}" + (f" task #{task} (run {run or '?'})" if task else "")
    return who + (f" {what}" if what else "")


def paths(n: int | None = None) -> list[Path]:
    return locks.slot_paths(slot_dir(), RESOURCE, limit() if n is None else n)


def priority_path(key: str) -> Path:
    """The priority slot of one push branch; `key` names the branch host-wide (push.priority_key)."""
    return slot_dir() / f"{PRIORITY}.{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock"


def priority_paths() -> list[Path]:
    try:
        return sorted(slot_dir().glob(f"{PRIORITY}.*.lock"))
    except OSError:
        return []


def state() -> dict:
    """Who runs checks host-wide now and who waits: {"limit", "running": [labels], "queued": [labels],
    "priority": [labels of push checks on their priority slots]}."""
    n = limit()
    running = locks.held_labels(paths(n)) if n else []
    priority = locks.held_labels(priority_paths()) if n else []
    queued = []
    for t in locks.queued(slot_dir(), RESOURCE):
        try:
            queued.append(t.read_text().strip())
        except OSError:
            continue
    return {"limit": n, "running": running, "queued": queued, "priority": priority}


def line() -> str:
    """One status line, or "" when nothing runs or waits."""
    s = state()
    if not s["running"] and not s["queued"] and not s["priority"]:
        return ""
    cap = f"cap {s['limit']}" if s["limit"] else "no cap"
    out = f"host checks: {len(s['running'])} running ({cap})"
    if s["priority"]:
        out += f", push priority slot: {_some(s['priority'])}"
    if s["queued"]:
        out += f", {len(s['queued'])} queued: " + _some(s["queued"])
    if s["running"]:
        out += f" (running: {_some(s['running'])})"
    return out


def _some(labels: list[str]) -> str:
    return "; ".join(labels[:4]) + ("; ..." if len(labels) > 4 else "")


class Slot:
    """A held slot (or none: cap off or nested); `close()` releases it."""

    def __init__(self, f=None):
        self.f = f

    def env(self) -> dict[str, str]:
        """For the check commands' environment: a `ttp checks` or push nested in them runs at once."""
        return {HELD_ENV: "1"} if self.f is not None else {}

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def acquire(holder: str, what: str = "", tell: Callable[[str], None] | None = None,
            waiting: Callable[[bool], None] | None = None, max_wait_s: float | None = None,
            since: float | None = None) -> Slot | None:
    """Take a host-wide checks slot, waiting in arrival order while all are held. `tell` gets a line
    when the wait starts and every TELL_S while it lasts; `waiting(True)` once it starts and
    `waiting(False)` once it ends. With `max_wait_s` it gives its place back and returns None once
    it waited that long. `since` is the arrival time to queue at (default now). A slot directory that cannot be written runs the checks uncapped: the cap
    must never stop them."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    ps = paths(n)
    try:
        f = locks.try_take(ps, holder, what) if not locks.queued(slot_dir(), RESOURCE) else None
        if f:
            return Slot(f)
        ticket = locks.enqueue(slot_dir(), RESOURCE, holder, since)
    except OSError as e:
        if tell:
            tell(f"host-wide checks cap not applied: {slot_dir()} is not writable ({e.strerror or e})")
        return Slot()
    started, told = time.time(), 0.0
    try:
        while True:
            try:
                f = locks.take_in_turn(slot_dir(), RESOURCE, ticket, ps, holder, what)
            except OSError:
                f = None
            if f:
                if told and tell:
                    tell(f"host-wide checks slot taken after {(time.time() - started) / 60:.1f} min")
                return Slot(f)
            if time.time() - told >= TELL_S:
                if tell:
                    q = locks.queued(slot_dir(), RESOURCE)
                    ahead = q.index(ticket) if ticket in q else 0
                    tell(f"queued for a host-wide checks slot: {len(locks.held_labels(ps))} of {n} running"
                         + (f", {ahead} ahead" if ahead else ""))
                if not told and waiting:
                    waiting(True)
                told = time.time()
            if max_wait_s is not None and time.time() - started >= max_wait_s:
                return None
            time.sleep(poll_s(POLL_S))
    finally:
        locks.dequeue(ticket)
        if told and waiting:
            waiting(False)


def acquire_priority(key: str, holder: str, what: str = "", tell: Callable[[str], None] | None = None) -> Slot:
    """Take the priority slot of the push branch `key` (beyond the cap, ahead of every task waiting in
    the queue), saying so through `tell`. Only another push to the same branch can hold it; then this
    waits for that one alone. Cap off, nested or an unwritable slot directory: no slot, run at once."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    path = priority_path(key)
    started, told = time.time(), 0.0
    while True:
        try:
            f = locks.try_take([path], holder, what)
        except OSError as e:
            if tell:
                tell(f"host-wide checks cap not applied: {slot_dir()} is not writable ({e.strerror or e})")
            return Slot()
        if f:
            if tell:
                q = len(locks.queued(slot_dir(), RESOURCE))
                tell(f"push checks took the push priority slot (beyond the host-wide cap of {n}: "
                     f"{len(locks.held_labels(paths(n)))} task checks running"
                     + (f", {q} queued behind" if q else "") + ")"
                     + (f" after {(time.time() - started) / 60:.1f} min" if told else ""))
            return Slot(f)
        if tell and time.time() - told >= TELL_S:
            tell(f"waiting for the push priority slot, held by another push to the same branch: "
                 f"{_some(locks.held_labels([path])) or 'unknown'}")
            told = time.time()
        time.sleep(poll_s(POLL_S))
