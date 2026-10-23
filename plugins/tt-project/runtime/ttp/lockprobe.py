# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The answer of `ttp lock --probe <resource>`, in one place: the probe prints it, and a task waiting on
such probes has its note built from it (Daemon._wait_what), so the two always name the same live
holders (locks.holders_now: pid and liveness, a done task's hold marked stale), never the holders a
hand-off once saw."""
from __future__ import annotations

import sys
from pathlib import Path

from . import locks


def pause(p, cfg: dict, resource: str) -> dict | None:
    """The pause that holds `resource` (a user pause of any of the device's names, or a fleet hold of its
    machine), else None. An unreadable database is no pause: it must not stop device commands."""
    try:
        from . import fleethold
        paused = p.db.paused_resources()
        res, hold = locks.canonical(cfg, resource), fleethold.covering(resource)
        return paused.get(resource) or next((v for k, v in paused.items() if locks.canonical(cfg, k) == res), None) \
            or (fleethold.entry(hold) if hold else None)
    except Exception as e:
        print(f"ttp lock: could not check for a pause of {resource}: {e}", file=sys.stderr, flush=True)
        return None


def line(resource: str, free: bool, locks_dir: Path, res: str, paths: list[Path], where=None) -> str:
    """"<resource>: free", or "<resource>: busy (held by <live holders>; N waiting[; reserved for X])"."""
    if free:
        return f"{resource}: free"
    n, who_r = len(locks.queued(locks_dir, res)), locks.reserved_by(locks.reserve_path(locks_dir, res))
    return (f"{resource}: busy (held by {', '.join(locks.holders_now(paths, where)) or 'nobody'}; {n} waiting"
            f"{f'; reserved for {who_r}' if who_r else ''})")


def answer(p, cfg: dict, resource: str, shared_mode: bool = False) -> tuple[int, str]:
    """(exit code, line) of `ttp lock --probe [--shared] <resource>` in project p: 0 free, 75 busy or
    paused. --shared on a device lock answers as the plain probe does."""
    from . import shared
    if (held := pause(p, cfg, resource)) is not None:
        return 75, f"{resource}: paused" + (f" ({held['reason']})" if held.get("reason") else "")
    res = locks.canonical(cfg, resource)
    if res in locks.device_locks(cfg) or res.endswith("-device") or resource.endswith("-device"):
        shared_mode = False
    where = shared.locks_dir(p, res, cfg)
    paths = locks.slot_paths(where, res, shared.slots(p, res, cfg))
    free = locks.probe(where, res, paths, shared=shared_mode)
    return (0 if free else 75), line(resource, free, where, res, paths, p)
