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

A slot directory that cannot be written (a worker's sandbox without it among its writable roots,
or file permissions) never runs the checks uncapped: acquire raises Unavailable, which says what to
fix. Workers get the directory as a writable root (daemon.start_run).

The cap is the account setting `checks.host_max` (`ttp config --account checks.host_max N`), or the
environment's TTP_CHECKS_HOST_MAX; 0 turns it off. Unset, it is one slot per CORES_PER_SLOT cores,
at least one. Check commands run with HELD_ENV set, so a `ttp checks` nested in them runs at once
instead of waiting for the slot its parent holds.

Push checks (the push queue's batches and `ttp push` to the push branch) hold the push lock, so every
approved change waits on them: they never queue behind task checks. Each push branch has one priority
slot of its own beyond the cap (`acquire_priority`), keyed by the remote and branch so it is one per
branch host-wide, across projects. Task checks keep their cap and their order: the priority slot never
takes one of theirs. It is an OS file lock like the others, so a killed push frees it too.

While the host is lightly loaded (1-minute load average under half its cores) up to `checks.load_extra`
slots (TTP_CHECKS_LOAD_EXTRA, default EXTRA_SLOTS; 0 turns it off) open beyond the cap: two below a quarter of the cores, one
below half. A check on one keeps it until it ends, but no new one takes one once the load rises.

A waiter whose checks match ones running now (`same`: the project, tree, commands and purpose) waits
for their result outside the queue, keeping its arrival time; `done` ends any wait once a recorded
pass makes the run needless, and `stop` ends it when the task the checks belong to ended.
"""
from __future__ import annotations

import hashlib
import json
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
EXTRA_SLOTS = 2   # at most this many slots beyond the cap while the host is lightly loaded (extra())


def slot_dir() -> Path:
    return project.HOME_DIR / "check-slots"


class Unavailable(RuntimeError):
    """The slot directory cannot be written here: the checks must not run without the cap."""


def _unavailable(e: OSError) -> Unavailable:
    return Unavailable(f"host-wide checks slots unavailable: {slot_dir()} is not writable here "
                       f"({e.strerror or e}); not running the checks without the host-wide cap. The run's "
                       f"sandbox or file permissions must allow writing it; hand the task back as blocked "
                       f"and name this directory")


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


def extra_max() -> int:
    """Slots that may open beyond the cap: TTP_CHECKS_LOAD_EXTRA, else the account's checks.load_extra,
    else EXTRA_SLOTS; an unreadable value is the default."""
    for v in (os.environ.get("TTP_CHECKS_LOAD_EXTRA"), (project.load_account_settings().get("checks") or {}).get("load_extra")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return EXTRA_SLOTS


def _load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return float("inf")


def extra(n: int | None = None) -> int:
    """Slots open beyond the cap now: two while the 1-minute load average is under a quarter of the
    cores, one under half, none above that, with the cap off or with checks.load_extra 0."""
    top = extra_max()
    if not (limit() if n is None else n) or not top:
        return 0
    load, c = _load(), cores()
    return min(top, 2 if load < c / 4 else 1 if load < c / 2 else 0)


class Cancelled(RuntimeError):
    """`stop` ended the wait: the checks' task ended while they waited (the reason is the message)."""


def label(project_name: str | None, what: str = "") -> str:
    """Who holds or waits for a slot, as status lines show it: the project and the task's run."""
    task, run = os.environ.get("TTP_TASK"), os.environ.get("TTP_RUN_ID")
    who = f"{project_name or 'no project'}" + (f" task #{task} (run {run or '?'})" if task else "")
    return who + (f" {what}" if what else "")


def task_priority(p) -> int | None:
    """The priority of the task this process runs for (TTP_TASK) in project `p`; None without one."""
    task = os.environ.get("TTP_TASK") or ""
    if p is None or not task.isdigit():
        return None
    try:
        t = p.db.task(int(task))
        return int(t["priority"]) if t and t.get("priority") is not None else None
    except Exception:   # an unreadable database only loses the ordering, never the slot
        return None


def paths(n: int | None = None) -> list[Path]:
    return locks.slot_paths(slot_dir(), RESOURCE, limit() if n is None else n)


def all_paths(n: int | None = None) -> list[Path]:
    """The cap's slots and then the ones load may open beyond it (extra()), held or not."""
    n = limit() if n is None else n
    return paths(n + extra_max()) if n else []


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
    running = locks.held_labels(all_paths(n)) if n else []
    priority = locks.held_labels(priority_paths()) if n else []
    queued, ahead, groups = [], {}, set()
    for t in locks.fair_order(slot_dir(), RESOURCE):
        try:
            queued.append(t.read_text().strip())
        except OSError:
            continue
        if (g := locks.ticket_group(t) or t.name) not in groups:   # each project's next waiter
            groups.add(g)
            ahead[_project(queued[-1])] = len(queued) - 1
    return {"limit": n, "extra": extra(n), "running": running, "queued": queued, "priority": priority,
            "ahead": ahead}


def _project(holder: str) -> str:
    """The project a holder label names (label())."""
    return holder.split(" task #", 1)[0].split(" ttp checks", 1)[0]


def line() -> str:
    """One status line, or "" when nothing runs or waits."""
    s = state()
    if not s["running"] and not s["queued"] and not s["priority"]:
        return ""
    cap = (f"cap {s['limit']}" + (f" +{s['extra']} while the load is low" if s["extra"] else "")
           if s["limit"] else "no cap")
    out = f"host checks: {len(s['running'])} running ({cap})"
    if s["priority"]:
        out += f", push priority slot: {_some(s['priority'])}"
    if s["queued"]:
        out += f", {len(s['queued'])} queued: " + _some(s["queued"])
    if s["running"]:
        out += f" (running: {_some(s['running'])})"
    if s["ahead"]:
        out += "; next per project: " + ", ".join(f"{k} {v} ahead" for k, v in list(s["ahead"].items())[:6])
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
            since: float | None = None, project_name: str | None = None,
            priority: int | None = None, same: str = "", done: Callable[[], bool] | None = None,
            stop: Callable[[], str] | None = None) -> Slot | None:
    """Take a host-wide checks slot, waiting while all are held. Waiters take turns by project
    (`project_name`; none form their own group), within one by task priority (`priority`, 1 first;
    none last), then arrival (locks.fair_order), so one project's burst cannot starve the others' checks. `tell` gets a line
    when the wait starts and every TELL_S while it lasts; `waiting(True)` once it starts and
    `waiting(False)` once it ends. With `max_wait_s` it gives its place back and returns None once
    it waited that long. `since` is the arrival time to queue at (default now). A slot directory that
    cannot be written raises Unavailable: the checks never run uncapped.

    `same` keys these checks (project, tree, commands, purpose): while checks with the same key hold a
    slot it waits for them outside the queue, keeping `since`, and queues again once they end. `done()`
    true (their pass was recorded) ends any wait with no slot held, Slot(); a reason from `stop()`
    ends it with Cancelled. Slots beyond the cap open while the host is lightly loaded (extra())."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    since = time.time() if since is None else since
    ps, every = paths(n), all_paths(n)
    try:
        f = None if locks.queued(slot_dir(), RESOURCE) or _running(every, same) else _take(n, ps, every, holder, what, same)
        if f:
            locks.mark_served(slot_dir(), RESOURCE, project_name)
            return Slot(f)
        ticket = locks.enqueue(slot_dir(), RESOURCE, holder, since, group=project_name, priority=priority)
    except OSError as e:
        raise _unavailable(e) from e
    started, told, checked = time.time(), 0.0, time.time()
    try:
        while True:
            if done and (ticket is None or time.time() - checked >= DONE_S):
                checked = time.time()
                if done():
                    return Slot()
            if stop and (why := stop()):
                raise Cancelled(why)
            twin = _running(every, same)
            if twin and ticket is not None:
                # Same checks run now: wait for their result instead of a place a slot frees to.
                locks.dequeue(ticket)
                ticket = None
                if tell:
                    tell(f"the same checks run now ({twin}); waiting for their result, keeping this place in the queue")
            elif not twin and ticket is None:
                try:
                    ticket = locks.enqueue(slot_dir(), RESOURCE, holder, since, group=project_name,
                                           priority=priority)
                except OSError as e:
                    raise _unavailable(e) from e
                if done and done():
                    return Slot()
            f = None
            if ticket is not None:
                try:
                    f = _take_in_turn(n, ticket, ps, every, holder, what, same)
                except OSError:
                    f = None
            if f:
                locks.mark_served(slot_dir(), RESOURCE, project_name)
                if told and tell:
                    tell(f"host-wide checks slot taken after {(time.time() - started) / 60:.1f} min")
                return Slot(f)
            if time.time() - told >= TELL_S:
                if tell and ticket is not None:
                    q = locks.fair_order(slot_dir(), RESOURCE)
                    ahead = q.index(ticket) if ticket in q else 0
                    groups = len({locks.ticket_group(t) for t in q} | {locks.ticket_group(ticket)})
                    tell(f"queued for a host-wide checks slot: {len(locks.held_labels(every))} of {n + extra(n)} running, "
                         f"{ahead} ahead, {groups} project{'s' if groups != 1 else ''}")
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


DONE_S = 30.0   # how often a waiter reads whether a recorded pass made its run needless


def _running(every: list[Path], same: str) -> str:
    """The holder of a slot whose checks have the key `same`, or ""."""
    if not same:
        return ""
    for path in every:
        if not locks.held_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict) and h.get("key") == same:
            return str(h.get("holder") or "")
    return ""


def _room(n: int, every: list[Path]) -> int:
    return n + extra(n) - len(locks.held_labels(every))


def _take(n: int, ps: list[Path], every: list[Path], holder: str, what: str, same: str, room: int | None = None):
    """A slot of the cap, else one load opened beyond it, while fewer than cap plus extra() are held.
    The cap's own slots are tried first, so two takers racing never pass the cap with no extra open."""
    room = _room(n, every) if room is None else room
    if room <= 0:
        return None
    f = locks.try_take(ps, holder, what, key=same)
    if f is None and (x := extra(n)):
        f = locks.try_take(every[n:n + x], holder, what, key=same)
    return f


def _take_in_turn(n: int, ticket: Path, ps: list[Path], every: list[Path], holder: str, what: str, same: str):
    """locks.take_in_turn over the cap and the slots load opened beyond it."""
    room = _room(n, every)
    if room <= 0 or not locks.my_turn(slot_dir(), RESOURCE, ticket, room, fair=True):
        return None
    return _take(n, ps, every, holder, what, same, room)


def acquire_priority(key: str, holder: str, what: str = "", tell: Callable[[str], None] | None = None) -> Slot:
    """Take the priority slot of the push branch `key` (beyond the cap, ahead of every task waiting in
    the queue), saying so through `tell`. Only another push to the same branch can hold it; then this
    waits for that one alone. Cap off or nested: no slot, run at once. An unwritable slot directory
    raises Unavailable."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    path = priority_path(key)
    started, told = time.time(), 0.0
    while True:
        try:
            f = locks.try_take([path], holder, what)
        except OSError as e:
            raise _unavailable(e) from e
        if f:
            if tell:
                q = len(locks.queued(slot_dir(), RESOURCE))
                tell(f"push checks took the push priority slot (beyond the host-wide cap of {n}: "
                     f"{len(locks.held_labels(all_paths(n)))} task checks running"
                     + (f", {q} queued behind" if q else "") + ")"
                     + (f" after {(time.time() - started) / 60:.1f} min" if told else ""))
            return Slot(f)
        if tell and time.time() - told >= TELL_S:
            tell(f"waiting for the push priority slot, held by another push to the same branch: "
                 f"{_some(locks.held_labels([path])) or 'unknown'}")
            told = time.time()
        time.sleep(poll_s(POLL_S))
