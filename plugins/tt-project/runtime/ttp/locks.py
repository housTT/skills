# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slots of a shared resource (a device, a remote build directory) as OS file locks.

Every holder takes one slot file under state/locks: `ttp lock` for one command, the run supervisor
of an `exclusive:<name>` task for its whole run. A lock ends with the process that holds it, so a
crash or a reboot never leaves a resource taken.

Waiters queue in arrival order (a ticket file per waiting process under <resource>.queue): only the
first `slots` live tickets may take a slot, so a waiter is never starved by later arrivals. A ticket
whose process is gone is dropped.

A shared holder (`ttp lock --shared`, for read-only work) takes a read lock on every slot instead
of one slot's write lock, and labels itself in <resource>.shared: shared holders run together, and
any slot's exclusive holder keeps them all out, as they keep it out. The queue stays in arrival
order across modes: a shared waiter goes only once no exclusive waiter is ahead of it, and an
exclusive one only once no shared waiter is.

Resources named in config `device.locks` are one device: they all map to the first name's lock, so
at most one command at a time is in the device phase, whatever name it locks by.

An exclusive task that finds every slot held reserves the resource: new `ttp lock` commands wait
until it has its slot, so commands that keep taking the lock in turn cannot starve it. Whoever
reserves refreshes the reservation while it waits; one not refreshed for RESERVE_STALE_S no longer
counts, so a crashed daemon or supervisor never wedges the resource.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import timefmt
from .project import durable_write, zombie

RESERVE_STALE_S = 120


def _device_list(cfg: dict) -> list[str]:
    return [str(x) for x in ((cfg.get("device") or {}).get("locks") or [])]


def device_locks(cfg: dict) -> set[str]:
    return set(_device_list(cfg))


def canonical(cfg: dict, resource: str) -> str:
    """The lock a resource name is held by: every device name shares the first one's lock."""
    dev = _device_list(cfg)
    return dev[0] if resource in dev else resource


def slot_paths(locks_dir: Path, resource: str, slots: int) -> list[Path]:
    return [Path(locks_dir) / f"{resource}.{i}.lock" for i in range(max(int(slots or 1), 1))]


def try_take(paths: list[Path], holder: str, what: str = "", key: str = ""):
    """The open file of the first free slot, now held and labelled with its holder (and `key`, what
    the holder runs, when given); None if all are taken."""
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"holder": holder, "since": time.time(), "command": what[:300], **({"key": key} if key else {})}))
        f.flush()
        return f
    return None


SHARED_MARK = " (shared)"   # ends a shared holder's label


def shared_dir(paths: list[Path]) -> Path:
    """Where the shared holders of the resource whose slots are `paths` label themselves."""
    return paths[0].parent / f"{paths[0].name.rsplit('.', 2)[0]}.shared"


class SharedHold:
    """A shared hold: read locks on every slot and the holder's label; close() ends it."""

    def __init__(self, files: list, label: Path):
        self.files, self.label = files, label

    def close(self) -> None:
        try:
            self.label.unlink()
        except OSError:
            pass
        for f in self.files:
            f.close()


def try_take_shared(paths: list[Path], holder: str, what: str = "") -> SharedHold | None:
    """A shared hold of the resource: a read lock on every slot, so no exclusive holder has one;
    None if one has. The label ends in SHARED_MARK."""
    files = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            f.close()
            for g in files:
                g.close()
            return None
        files.append(f)
    d = shared_dir(paths)
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = f"{time.time():017.6f}-{os.getpid()}"
        # Locked under a hidden name first, then renamed in: a reader never sees it unlocked.
        tmp = d / f".{name}.tmp"
        lf = open(tmp, "a+")
        fcntl.flock(lf, fcntl.LOCK_EX)
        lf.write(json.dumps({"holder": holder + SHARED_MARK, "since": time.time(), "command": what[:300]}))
        lf.flush()
        os.rename(tmp, d / name)
    except OSError:
        for g in files:
            g.close()
        raise
    return SharedHold(files + [lf], d / name)


def shared_holders(paths: list[Path]) -> list[dict]:
    """The labels ({holder, since, command}) of the resource's live shared holders, oldest first;
    labels whose process is gone are removed."""
    d, out = shared_dir(paths), []
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return out
    for name in names:
        path = d / name
        if not _ticket_live(path):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            out.append(json.loads(path.read_text() or "{}"))
        except (OSError, ValueError):
            continue
    return out


def any_free(paths: list[Path]) -> bool:
    """Whether some slot is free right now. Testing takes the slot for an instant; a `ttp lock`
    that tries in that instant just retries."""
    for path in paths:
        if not path.exists():
            return True
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            return True
    return False


def holders(paths: list[Path], where=None) -> list[str]:
    """Each slot's holder label and since when, in the zone of `where` (the project; None is UTC)."""
    out = []
    for path in paths:
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if h:
            out.append(f"{h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}")
    return out


def held_labels(paths: list[Path]) -> list[str]:
    """The holder labels of the slots among paths whose lock is taken right now, then the
    resource's shared holders."""
    return _slot_labels(paths) + [str(h.get("holder") or "") for h in shared_holders(paths)] if paths else []


def holders_now(paths: list[Path], where=None) -> list[str]:
    """`holders`, of only the slots whose lock is taken right now, then the shared holders."""
    out = holders([x for x in paths if _slot_labels([x])], where)
    return out + [f"{h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}"
                  for h in (shared_holders(paths) if paths else [])]


def _slot_labels(paths: list[Path]) -> list[str]:
    """The holder labels of the slots among paths whose lock is taken right now (not shared)."""
    out = []
    for path in paths:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue
                except OSError:
                    pass
            out.append(str(json.loads(path.read_text() or "{}").get("holder") or ""))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def held(locks_dir: Path, where=None) -> list[str]:
    """Who holds each resource right now, and who has one reserved: "device: task #3 (run 9) since
    10:02 UTC" (in the zone of `where`, the project). A slot file keeps its label after release, so only slots whose lock is taken count;
    testing takes a free one for an instant, which a `ttp lock` trying then just retries."""
    out, locks_dir = [], Path(locks_dir)
    try:
        slots = sorted(locks_dir.glob("*.lock"))
        marks = sorted(locks_dir.glob("*.reserved"))
    except OSError:
        return out
    for path in slots:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue
                except OSError:
                    pass
        except OSError:
            continue
        out += [f"{path.name.rsplit('.', 2)[0]}: {h}" for h in holders([path], where)]
    try:
        groups = sorted(locks_dir.glob("*.shared"))
    except OSError:
        groups = []
    for d in groups:
        res = d.name[:-len(".shared")]
        out += [f"{res}: {h.get('holder')} since {timefmt.short(float(h.get('since') or 0), where)}"
                for h in shared_holders([d.parent / f"{res}.0.lock"])]
    for path in marks:
        who = reserved_by(path)
        if who:
            out.append(f"{path.name[:-len('.reserved')]}: reserved for {who}")
    return out


def reserve_path(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.reserved"


def reserve(path: Path, holder: str) -> None:
    """Reserve for holder, or refresh its reservation. A fresh one by another holder stays."""
    other = reserved_by(path)
    if other and other != holder:
        return
    try:
        since = json.loads(path.read_text()).get("since") if other else None
    except (OSError, ValueError):
        since = None
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_write(path, json.dumps({"holder": holder, "since": since or time.time(), "ts": time.time()}))


def reserved_by(path: Path) -> str | None:
    """The holder of a live reservation; None when there is none or it went stale."""
    try:
        r = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None
    age = time.time() - float(r.get("ts") or 0)
    # A stamp from the future means the clock went back; trusting it would hold the resource
    # until the clock caught up.
    if age > RESERVE_STALE_S or age < -5:
        return None
    return str(r.get("holder") or "") or None


def unreserve(path: Path, holder: str) -> None:
    try:
        if json.loads(path.read_text() or "{}").get("holder") == holder:
            path.unlink()
    except (OSError, ValueError):
        pass


WAITS_FILE = "lock_waits.json"


def record_wait(run_dir: Path, key: str, start: float, end: float | None) -> None:
    """Record one `ttp lock` wait of a run: open (end None) while it waits, closed once it ends.
    Several commands of one run may wait at once; the flock keeps their updates from overwriting
    each other, and the rename keeps readers from seeing half a file."""
    run_dir = Path(run_dir)
    try:
        with open(run_dir / f"{WAITS_FILE}.lock", "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                waits = json.loads((run_dir / WAITS_FILE).read_text())
            except (OSError, ValueError):
                waits = {}
            waits[key] = {"start": start, "end": end, "pid": os.getpid()}
            durable_write(run_dir / WAITS_FILE, json.dumps(waits))
    except OSError:
        pass    # a missing record only costs the run its extension, never the command


def waited(run_dir: Path, now: float | None = None) -> float:
    """Seconds the run has spent waiting in `ttp lock`, overlapping waits counted once. A wait still
    going counts up to now; one whose process died without closing it counts nothing."""
    now = time.time() if now is None else now
    try:
        waits = json.loads((Path(run_dir) / WAITS_FILE).read_text())
    except (OSError, ValueError):
        return 0.0
    spans = []
    for w in waits.values():
        try:
            start, end = float(w["start"]), w.get("end")
            if end is None:
                end = now if _alive(int(w.get("pid") or 0)) else start
            spans.append((start, min(float(end), now)))
        except (KeyError, TypeError, ValueError):
            continue
    total, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not zombie(pid)


def queue_dir(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.queue"


_TICKETS: dict[Path, object] = {}   # this process's tickets: the open file that holds each one's lock


SHARED_TICKET = "-shared"   # ends a shared waiter's ticket name
GROUP_TICKET = ".g"         # then a fair queue's group id: <arrival>-<pid>[.p<priority>].g<id>
PRIORITY_TICKET = ".p"      # before it, the waiter's task priority (lowest first; checkcap uses 0 for obligation owners)
SERVED_KEEP_S = 86400.0     # a group's last-served time older than this no longer changes its turn


def group_id(group: str | None) -> str:
    """The short id a fair queue's ticket carries for its group (a project); "" for none."""
    return hashlib.sha256(group.encode()).hexdigest()[:12] if group else ""


def enqueue(locks_dir: Path, resource: str, holder: str, since: float | None = None,
            shared: bool = False, group: str | None = None, priority: int | None = None) -> Path:
    """A ticket for this process in the resource's arrival queue. Like a slot, it is an OS file lock
    held by this process, so it ends with it (a crash, a kill, a reboot) whatever its pid becomes.
    Not written durably: a power cut ends every waiter with it. Remove it with `dequeue`. `since`
    is the arrival time to queue at (default now): a waiter that hands over keeps its place.
    A shared waiter's ticket name ends in SHARED_TICKET. `group` (a project) is what a fair queue
    (fair_order) takes turns between; waiters without one form their own group. `priority` (the task's;
    lower first) orders the waiters within a group; it goes before the group id, so older runtimes
    still read the group."""
    d = queue_dir(locks_dir, resource)
    d.mkdir(parents=True, exist_ok=True)
    gid = group_id(group)
    prio = f"{PRIORITY_TICKET}{int(priority)}" if priority is not None else ""
    name = (f"{time.time() if since is None else since:017.6f}-{os.getpid()}{prio}"
            f"{GROUP_TICKET + gid if gid else ''}{SHARED_TICKET if shared else ''}")
    # Locked under a hidden name first, then renamed in: `queued` never sees it unlocked.
    tmp = d / f".{name}.tmp"
    f = open(tmp, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(holder)
    f.flush()
    path = d / name
    os.rename(tmp, path)
    _TICKETS[path] = f
    return path


def dequeue(ticket: Path | None) -> None:
    if ticket is None:
        return
    try:
        ticket.unlink()
    except OSError:
        pass
    f = _TICKETS.pop(ticket, None)
    if f is not None:
        f.close()


def _ticket_live(path: Path) -> bool:
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
    except OSError:
        return False


def queued(locks_dir: Path, resource: str) -> list[Path]:
    """Live tickets in arrival order; tickets whose process is gone are removed."""
    d = queue_dir(locks_dir, resource)
    out = []
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return out
    for name in names:
        path = d / name
        if path in _TICKETS or _ticket_live(path):
            out.append(path)
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return out


def is_shared(ticket: Path) -> bool:
    return ticket.name.endswith(SHARED_TICKET)


def ticket_group(ticket: Path) -> str:
    """A ticket's group id ("" for none)."""
    name = ticket.name.removesuffix(SHARED_TICKET)
    return name.rpartition(GROUP_TICKET)[2] if GROUP_TICKET in name else ""


def ticket_priority(ticket: Path) -> float:
    """A ticket's task priority; one without (older runtimes, no task) sorts after every priority."""
    name = ticket.name.removesuffix(SHARED_TICKET).partition(GROUP_TICKET)[0]
    try:
        return float(int(name.rpartition(PRIORITY_TICKET)[2])) if PRIORITY_TICKET in name else math.inf
    except ValueError:
        return math.inf


def _arrival(ticket: Path) -> float:
    try:
        return float(ticket.name.split("-", 1)[0])
    except ValueError:
        return 0.0


def served_dir(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.served"


def mark_served(locks_dir: Path, resource: str, group: str | None) -> None:
    """Record that `group` just got the resource, so a fair queue serves the other groups first.
    Only a time: a stale or lost record just makes that group look longer unserved."""
    d = served_dir(locks_dir, resource)
    now = time.time()
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / (group_id(group) or "none")
        f.touch()
        os.utime(f, (now, now))
        for old in d.iterdir():
            if now - old.stat().st_mtime > SERVED_KEEP_S:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _served(locks_dir: Path, resource: str) -> dict[str, float]:
    out = {}
    try:
        for f in served_dir(locks_dir, resource).iterdir():
            out["" if f.name == "none" else f.name] = f.stat().st_mtime
    except OSError:
        pass
    return out


def fair_order(locks_dir: Path, resource: str, q: list[Path] | None = None) -> list[Path]:
    """The waiters in the order a fair queue serves them: each next turn goes to the oldest waiter
    of the group that has waited longest since it last got the resource (since its oldest waiter
    arrived, if later), so one group's burst cannot starve the others. Within a group by task priority
    (ticket_priority), then arrival.

    Known gap: while some projects on a host still run a runtime from before fair ordering
    (arrival-order my_turn) next to fair-order waiters, the two orders can disagree on who goes
    first, so one slot may sit idle until another frees; this ends once every project on the host
    is upgraded."""
    q = queued(locks_dir, resource) if q is None else q
    groups: dict[str, list[Path]] = {}
    for t in q:
        groups.setdefault(ticket_group(t), []).append(t)
    served = _served(locks_dir, resource)
    key = {g: (0, max(served.get(g, 0.0), _arrival(ts[0])), ts[0].name) for g, ts in groups.items()}
    for ts in groups.values():
        ts.sort(key=ticket_priority)   # stable: arrival order within a priority
    out = []
    while groups:
        g = min(groups, key=key.__getitem__)
        out.append(groups[g].pop(0))
        if groups[g]:
            key[g] = (1, len(out), "")
        else:
            del groups[g]
    return out


def my_turn(locks_dir: Path, resource: str, ticket: Path, slots: int, fair: bool = False) -> bool:
    """Whether this ticket may take the resource now. An exclusive one: among the first `slots`
    waiters, one per free slot, with no shared waiter ahead. A shared one: no exclusive waiter
    ahead, so it never jumps one. `fair`: waiters are counted in fair_order, not arrival order."""
    q = queued(locks_dir, resource)
    if ticket not in q:
        return True
    if fair:
        q = fair_order(locks_dir, resource, q)
    ahead = q[:q.index(ticket)]
    if is_shared(ticket):
        return all(is_shared(t) for t in ahead)
    return not any(is_shared(t) for t in ahead) and len(ahead) < max(int(slots or 0), 0)


def take_in_turn(locks_dir: Path, resource: str, ticket: Path, paths: list[Path], holder: str, what: str = "",
                 fair: bool = False):
    """try_take (try_take_shared for a shared ticket), but only when this ticket's turn has come
    (my_turn), so a later waiter never takes the one slot an earlier one is about to."""
    if is_shared(ticket):
        return try_take_shared(paths, holder, what) if my_turn(locks_dir, resource, ticket, 0) else None
    if not my_turn(locks_dir, resource, ticket, len(paths) - len(held_labels(paths)), fair):
        return None
    return try_take(paths, holder, what)


def probe(locks_dir: Path, resource: str, paths: list[Path], shared: bool = False) -> bool:
    """Free now, nobody waiting and not reserved: a task that gave up waiting can come back. For a
    shared taker: no exclusive holder, no exclusive waiter and not reserved."""
    if reserved_by(reserve_path(locks_dir, resource)):
        return False
    if shared:
        return not _slot_labels(paths) and all(is_shared(t) for t in queued(locks_dir, resource))
    return any_free(paths) and not queued(locks_dir, resource)


def job_lock(rc: Path) -> Path:
    """The lock a `ttp detach` job holds while any of its processes lives, next to its .rc file."""
    return Path(rc).with_suffix(".lock")


def job_ended(rc: Path) -> bool:
    """Whether a detached job ended: it wrote its exit code, or it is gone without one (detach.state);
    one the daemon is yet to start has not."""
    from .detach import state
    return state(Path(rc))[0] in ("ended", "lost")


def job_probe(rcs: list, ttp: str = "ttp") -> str:
    """A `retry_when` that exits 0 once every one of these detached jobs ended with exit 0, 4 once all
    ended but one failed or is gone (detach.FAILED_RC), 1 before."""
    return f"{ttp} detach --check " + " ".join(shlex.quote(str(r)) for r in rcs)


def _parent(pid: int) -> int | None:
    """The parent of process pid; None if it is gone or this cannot tell."""
    try:   # Linux: field 4
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def in_run(run_dir: Path | None) -> bool:
    """Whether this process still runs under the run's agent (child.pid in the run's folder), so the
    run supervises it. A background (`nohup ... &`) driver is re-parented once its shell exits and then is not.
    True whenever it cannot tell: another PID namespace (a sandbox), no record, an unreadable parent."""
    try:
        agent = int((run_dir / "child.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError, TypeError):
        return True
    if (ns := os.environ.get("TTP_PIDNS")):
        try:
            if os.readlink("/proc/self/ns/pid") != ns:
                return True
        except OSError:
            pass
    pid = os.getpid()
    for _ in range(256):
        if pid == agent:
            return True
        if pid <= 1:
            return False
        nxt = _parent(pid)
        if nxt is None:
            return True
        pid = nxt
    return True
