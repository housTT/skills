# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Planned host cycles: a reboot or power cycle of this host that a recovery owner chose to make.

Whoever cycles the host on purpose records it first, in <project state>/host-cycle/<id>/record.json
(`ttp drain <name> --reboot "<why>"` writes one): the boot it started on (`boot_id`), when
(`started`, epoch seconds), why (`reason`) and `dry_run` when nothing was cut. On the next boot
every project on this host reads the records of every project here, so a cycle another project
made counts too. A reboot that matches one was not caused by a task's detached job: that job's
loss does not count toward max_reboot_losses.

`ttp drain` also lists the project's `ttp detach` jobs still running here, which a reboot would
end, and the record names them as interrupted so the woken tasks and the user can see why.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import project
from .project import Project, write_json

FOLDER = "host-cycle"   # under a project's state
# A record matches a boot that came at most this long after it started: a record left by a cycle
# that never happened does not excuse a crash hours later on the same boot.
WINDOW_S = 7200


def _state_dirs(p: Project | None) -> list[Path]:
    """This project's state folder first, then every other registered project's on this host."""
    out = [p.state] if p else []
    here = project.hostname()
    for _, e in sorted((project.load_registry().get("projects") or {}).items()):
        if isinstance(e, dict) and e.get("host") in (None, here) and e.get("dir"):
            d = Path(e["dir"]) / project.FOLDER / "state"
            if d not in out:
                out.append(d)
    return out


def planned(p: Project | None, boot: str | None, boot_at: float | None) -> dict | None:
    """The record of a planned cycle that ended boot `boot` (the earlier boot), if one says so: not a
    dry run, started on that boot, and at most WINDOW_S before `boot_at` (this boot's start)."""
    if not boot:
        return None
    for d in _state_dirs(p):
        try:
            files = sorted((d / FOLDER).glob("*/record.json"), reverse=True)
        except OSError:
            continue
        for f in files:
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(r, dict) or r.get("boot_id") != boot or r.get("dry_run") or r.get("rebooted") is False:
                continue
            started = r.get("started")
            if boot_at and (not isinstance(started, (int, float)) or not 0 <= boot_at - started <= WINDOW_S):
                continue
            return {**r, "path": str(f)}
    return None


def running_jobs(p: Project, days: float = 7) -> list[dict]:
    """The project's `ttp detach` jobs still running (or queued for the daemon) on this boot: what a
    reboot of this host would end. A job driven by `ttp detach --remote` lives on the other host."""
    from .detach import state
    from .runner import boot_ids
    boots = set(boot_ids())
    out = []
    for r in p.db.q("SELECT id, task, dir FROM runs WHERE started>? AND dir IS NOT NULL ORDER BY id",
                    (time.time() - days * 86400,)):
        try:
            jobs = json.loads((Path(r["dir"]) / "detached.json").read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:
            if not isinstance(j, dict) or not j.get("rc") or (j.get("boot") and j["boot"] not in boots):
                continue
            if state(Path(j["rc"]))[0] in ("running", "queued"):
                out.append({"name": j.get("name"), "run": r["id"], "task": r["task"], "rc": j["rc"],
                            "log": j.get("log"), "command": j.get("command")})
    return out


def record(p: Project, reason: str, left_running: list[dict], jobs: list[dict]) -> Path:
    """Record a planned cycle of this host, about to happen: the next boot matches it (planned)."""
    from .runner import boot_id
    now = time.time()
    rid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    d = p.state / FOLDER / rid
    n = 1
    while d.exists():   # two drains within a second
        n += 1
        d = p.state / FOLDER / f"{rid}-{n}"
    d.mkdir(parents=True)
    f = d / "record.json"
    write_json(f, {"id": d.name, "started": now, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                   "boot_id": boot_id(), "dry_run": False, "reason": reason, "by": "ttp drain",
                   "left_running": left_running, "interrupted": jobs})
    return f


def describe(jobs: list[dict]) -> str:
    return ", ".join(f"{j['name']} (task #{j['task']}, log {j['log']})" if j.get("task") else
                     f"{j['name']} (run {j['run']}, log {j['log']})" for j in jobs)
