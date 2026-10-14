# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which host a resource lives on, and `ttp ssh-preflight` (exit 0 once a host answers over ssh).

A waiting task's probe that names a host keeps probing it after its resource moved to another box.
`ttp ssh-preflight @<resource>` looks the host up when it runs instead: in config `device.hosts`
({resource: host}), else the host of device runner <resource> (`device.runners.<name>.host`). A
device lock name (`device.locks`) also finds the entry of the name it shares the lock with. A plain
host is probed as it is. Moving a resource is then one config change, and every waiting probe
follows it.
"""
from __future__ import annotations

import subprocess

from . import locks

UNREACHABLE_RC = 255   # what ssh exits with; netwait counts it as the network, the task sleeps on
UNKNOWN_RC = 2         # a resource no map names: the probe is broken and wakes the task
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def table(cfg: dict) -> dict[str, str]:
    """resource -> host: device runners' hosts, overridden by `device.hosts`."""
    dev = (cfg or {}).get("device") or {}
    out: dict[str, str] = {}
    runners = dev.get("runners")
    for name, r in (runners.items() if isinstance(runners, dict) else []):
        if isinstance(r, dict) and isinstance(r.get("host"), str) and r["host"].strip():
            out[str(name)] = r["host"].strip()
    hosts = dev.get("hosts")
    for name, host in (hosts.items() if isinstance(hosts, dict) else []):
        if isinstance(host, str) and host.strip():
            out[str(name)] = host.strip()
    return out


def resolve(cfg: dict, target: str) -> str:
    """The host to reach: `@<resource>` from the map, anything else as it is. KeyError names an
    unknown resource and the ones the map has."""
    target = str(target or "").strip()
    if not target.startswith("@"):
        return target
    name = target[1:]
    tab = table(cfg)
    host = tab.get(name) or tab.get(locks.canonical(cfg, name))
    if not host:
        known = ", ".join(f"@{k}" for k in sorted(tab)) or "none"
        raise KeyError(f"no host for resource {name!r} in config device.hosts or device.runners "
                       f"(known: {known})")
    return host


def problems(cfg: dict) -> list[str]:
    """Config check of `device.hosts`."""
    hosts = ((cfg or {}).get("device") or {}).get("hosts")
    if hosts is None:
        return []
    if not isinstance(hosts, dict):
        return ["device.hosts: not a table of resource names to hosts"]
    return [f"device.hosts.{k}: {v!r} is not a host name" for k, v in hosts.items()
            if not isinstance(v, str) or not v.strip() or v.strip().startswith(("-", "@"))]


def preflight(host: str, timeout: float = 30.0) -> tuple[int, str]:
    """(exit code, ssh's output) of `ssh <host> true`: 0 reachable, 255 not."""
    try:
        r = subprocess.run([*SSH, host, "true"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return UNREACHABLE_RC, f"ssh: connect to host {host}: timed out after {timeout:g} s"
    except OSError as e:
        return UNREACHABLE_RC, f"ssh: {e}"
    return r.returncode, (r.stderr or r.stdout or "").strip()
