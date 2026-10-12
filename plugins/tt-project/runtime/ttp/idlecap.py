# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Idle capacity, model-free (Daemon.check_idle_capacity): a device the project holds sits unused.

A device resource of the project (candidates) is idle while no open task names it, no `ttp lock` slot
of it is taken or reserved and no detached-job hold keeps it. Paused resources (the user's, the
project's own or a shared one), resources in trouble and machines with an open ledger condition are
never candidates. Once one has been idle for `coordinator.idle_capacity_s` (1500 s; 0: off) while a
worker slot is free, the budget gate is green or yellow and dispatch would start nothing queued, one
normal `idle_capacity` event asks the coordinator to fill it with charter work or say once that
nothing useful remains.

Debounce (step): one event per idle stretch. A stretch ends when the device is used, paused or in
trouble again; the next one counts its grace afresh. A stretch still idle after its event raises again
only after `coordinator.idle_capacity_rearm_s` (6 h), doubling each time up to REARM_MAX_S, so a
project with nothing for its device costs a few turns a day at most. The state is kept in the
database, so a restart never raises a stretch twice.
"""
from __future__ import annotations

import json

from . import locks

KIND = "idle_capacity"
STATE_KEY = "idle_capacity"   # kv: {resource: {"since", "fired", "n"}} of the open idle stretches
GRACE_S = 1500.0
REARM_S = 6 * 3600.0
REARM_MAX_S = 48 * 3600.0
SUFFIX = "-device"
LABEL_LOOKBACK_S = 14 * 86400   # task labels this recent name the project's device resources


def settings(cfg: dict) -> tuple[float, float]:
    """(grace_s, rearm_s) from the `coordinator` config; grace 0 turns the check off."""
    c = cfg.get("coordinator") or {}
    grace, rearm = c.get("idle_capacity_s", GRACE_S), c.get("idle_capacity_rearm_s", REARM_S)
    return float(GRACE_S if grace is None else grace), float(REARM_S if rearm is None else rearm)


def labels(task: dict) -> list[str]:
    try:
        v = json.loads(task.get("labels") or "[]")
    except ValueError:
        return []
    return [lb for lb in v if isinstance(lb, str)] if isinstance(v, list) else []


def task_names(task: dict, cfg: dict) -> set[str]:
    """The resources a task names (`resource:`, `exclusive:`), device-lock aliases as their lock;
    `needs_device` as the configured device lock."""
    out = {locks.canonical(cfg, lb.split(":", 1)[1]) for lb in labels(task)
           if lb.split(":", 1)[0] in ("resource", "exclusive") and ":" in lb}
    dev = (cfg.get("device") or {}).get("locks") or []
    if dev and "needs_device" in labels(task):
        out.add(str(dev[0]))
    return out - {""}


def uses(name: str, resource: str) -> bool:
    """Whether a task or lock naming `name` uses device `resource`: the same name, or a machine alias
    and its `<alias>-device` resource."""
    return name == resource or name == resource + SUFFIX or resource == name + SUFFIX


def candidates(cfg: dict, named: set[str], resource_machines: list[str], machines: dict[str, dict]) -> set[str]:
    """The project's device resources: its configured device lock, every `*-device` name its config or
    recent tasks name, and each machine its charter's Resources names that is tagged `device`. A machine
    is dropped when its `<alias>-device` resource is there too: they are one device."""
    out = set()
    dev = (cfg.get("device") or {}).get("locks") or []
    if dev:
        out.add(str(dev[0]))
    shared = cfg.get("shared_resources") or []
    for name in {*named, *(cfg.get("resources") or {}), *(x for x in shared if isinstance(x, str))}:
        if str(name).endswith(SUFFIX):
            out.add(locks.canonical(cfg, str(name)))
    out |= {a for a in resource_machines if "device" in ((machines.get(a) or {}).get("tags") or [])}
    return {n for n in out if n and n + SUFFIX not in out}


def step(st: dict | None, idle: set[str], now: float, grace: float, rearm: float,
         may_fire: bool) -> tuple[dict, list[str]]:
    """The idle stretches after one check, and the resources to raise now.

    `idle`: the device resources idle right now. One that is not ends its stretch. `may_fire`: a slot
    is free, the gate allows new work and nothing queued would start; when false no event is raised,
    but the stretches go on (a busy slot does not make the device any less idle)."""
    old = st or {}
    new, fired = {}, []
    for name in sorted(idle):
        rec = dict(old.get(name) or {"since": now})
        if may_fire and grace > 0:
            n = int(rec.get("n") or 0)
            wait = grace if not rec.get("fired") else min(rearm * 2 ** max(n - 1, 0), REARM_MAX_S)
            start = float(rec["fired"]) if rec.get("fired") else float(rec["since"])
            if (n == 0 or rearm > 0) and now - start >= wait:
                rec.update(fired=now, n=n + 1)
                fired.append(name)
        new[name] = rec
    return new, fired


def text(stretches: dict, fired: list[str], now: float, slots: int, running: list[str], next_check: str,
         rearm: float) -> str:
    """The event: which devices sit idle and since when, what runs, the next check, what to do."""
    idle = "; ".join(f"{n} for {(now - float(stretches[n]['since'])) / 60:.0f} min" for n in fired)
    again = (f"again only after it is used or {rearm / 3600:g} h from now (longer each time)" if rearm > 0
             else "again only after it is used")
    return (f"Idle capacity: device resource(s) idle with nothing queued that uses them: {idle}. "
            f"{slots} worker slot(s) free. Running: {'; '.join(running) or 'nothing'}. "
            f"Next scheduled check: {next_check or 'none'}. Queue useful charter work for it, or say "
            f"once that nothing useful remains (no ask). This is raised {again}.")
