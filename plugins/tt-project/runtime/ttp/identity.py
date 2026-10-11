# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What code a running daemon loaded: its runtime identity receipt.

The daemon captures the receipt once, at startup, before its first tick: the version of the
imported package, the SOURCE_COMMIT next to it, the runtime path, pid, process start, boot id and a
random instance id. Nothing re-reads files later, and nothing infers the running version from git or
from the harness files: files on disk change under a running process, its imported code does not.
The receipt goes into the heartbeat and the start marker, so `ttp status`, the web app and
`ttp overview` can say what is really running, and when that differs from the files ("restart
pending"). A heartbeat without one (a daemon older than receipts) reads as unverified.

The time of the last completed tick is kept in its own file (TICK_FILE, its mtime), apart from the
heartbeat, which a long tick's steps also touch to tell the watchdogs it still moves.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

TICK_FILE = "tick_done"   # in state/: written once per daemon, its mtime is the last completed tick
SOURCE_FILE = "SOURCE_COMMIT"


def capture(mod=None, boot: str | None = None, started: float | None = None) -> dict:
    """The receipt of the code this process imported (`mod`: the ttp package). Provenance is
    'verified' only when SOURCE_COMMIT is there, the package's __init__.py on disk still names the
    imported version, and every loaded module of the package comes from its own directory."""
    if mod is None:
        import ttp as mod
    from .runner import boot_id, proc_start
    pkg = Path(mod.__file__).resolve().parent
    version = str(getattr(mod, "__version__", "") or "unknown")
    why = []
    try:
        commit = (pkg / SOURCE_FILE).read_text().strip()
    except OSError:
        commit = ""
    if not commit:
        why.append("no SOURCE_COMMIT")
    try:
        m = re.search(r'__version__ = "([^"]+)"', (pkg / "__init__.py").read_text())
        on_disk = m.group(1) if m else ""
    except OSError:
        on_disk = ""
    if on_disk != version:
        why.append(f"files say {on_disk or 'nothing'}, imported {version}")
    name = getattr(mod, "__name__", "ttp")
    stray = sorted(k for k, v in list(sys.modules.items())
                   if k.startswith(name + ".") and getattr(v, "__file__", None)
                   and Path(v.__file__).resolve().parent != pkg and pkg not in Path(v.__file__).resolve().parents)
    if stray:
        why.append("modules from elsewhere: " + ", ".join(stray[:3]))
    pid = os.getpid()
    return {"version": version, "commit": commit or None, "runtime": str(pkg.parent), "pid": pid,
            "proc_start": proc_start(pid), "started": started or time.time(), "boot": boot or boot_id(),
            "instance": secrets.token_hex(8), "provenance": "unverified" if why else "verified",
            **({"why": "; ".join(why)} if why else {})}


def mark_tick(state: Path, receipt: dict, first: bool) -> None:
    """A tick completed: the tick file's mtime moves to now. Written in full on this daemon's
    first tick (it names the instance, so another daemon's file is never read as this one's)."""
    f = state / TICK_FILE
    try:
        if first or not f.exists():
            f.write_text(json.dumps({"instance": receipt.get("instance"), "pid": receipt.get("pid")}))
        else:
            os.utime(f, None)
    except OSError:
        pass


def tick_done(state: Path, receipt: dict | None) -> float | None:
    """When the daemon with this receipt last completed a full tick; None if unknown."""
    if not receipt or not receipt.get("instance"):
        return None
    f = state / TICK_FILE
    try:
        if json.loads(f.read_text()).get("instance") != receipt["instance"]:
            return None
        return f.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        return None


def running(state: Path, pid: int) -> dict | None:
    """The receipt of daemon `pid` from its heartbeat, else from its start marker (a daemon still in
    its first tick); None when neither names that pid with a receipt (an older daemon)."""
    for name in ("heartbeat", "daemon.start"):
        try:
            d = json.loads((state / name).read_text())
        except (OSError, ValueError):
            continue
        r = d.get("receipt") if isinstance(d, dict) else None
        if isinstance(r, dict) and int(d.get("pid") or 0) == pid and int(r.get("pid") or 0) == pid:
            return r
    return None


def files_version(receipt: dict | None, fallback: Path) -> str:
    """The version a restart would load: the runtime the daemon was started from, else `fallback`."""
    from .release import runtime_version
    rt = Path(receipt["runtime"]) if receipt and receipt.get("runtime") else None
    return runtime_version(rt if rt and (rt / "ttp" / "__init__.py").is_file() else fallback)


def summary(state: Path, pid: int, fallback: Path, where=None, now: float | None = None) -> dict:
    """{running_version, running_commit, instance, started, tick_done, files_version, provenance,
    restart_pending, line} for daemon `pid` (0: none running). `line` is the one line `ttp status`
    shows, times in the home zone (`where`)."""
    from . import timefmt
    r = running(state, pid) if pid else None
    files = files_version(r, fallback)
    out = {"running_version": None, "running_commit": None, "instance": None, "started": None, "tick_done": None,
           "files_version": files, "provenance": None, "restart_pending": False, "line": ""}
    if not pid:
        return out
    if not r:
        out.update(running_version="unverified", provenance="unverified", line="running version unverified")
        return out
    commit = (r.get("commit") or "")[:12]
    out.update(running_version=r.get("version"), running_commit=r.get("commit"), instance=r.get("instance"),
               started=r.get("started"), tick_done=tick_done(state, r), provenance=r.get("provenance"))
    since = timefmt.short(float(r["started"]), where, now) if r.get("started") else "?"
    line = f"running {r.get('version')}" + (f" ({commit})" if commit else "") + f" since {since}"
    if r.get("provenance") != "verified":
        line += f" (provenance unverified: {r.get('why') or 'unknown'})"
    if files != "unknown" and r.get("version") != files:
        out["restart_pending"] = True
        line = f"restart pending: running {r.get('version')}, files {files} · " + line
    out["line"] = line
    return out
