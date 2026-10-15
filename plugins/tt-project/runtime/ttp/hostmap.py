# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which host a resource lives on, and `ttp ssh-preflight` (exit 0 once a host answers over ssh).

A waiting task's probe that names a host keeps probing it after its resource moved to another box.
`ttp ssh-preflight @<resource>` looks the host up when it runs instead: in config `device.hosts`
({resource: host}), else the host of device runner <resource> (`device.runners.<name>.host`). A
device lock name (`device.locks`) also finds the entry of the name it shares the lock with. A plain
host is probed as it is. Moving a resource is then one config change, and every waiting probe
follows it.

The probe checks the host itself: strict host-key checking (a user ssh config that turns checking off does not apply:
it cannot mask a missing key) and no connection sharing (an open ControlMaster connection would
answer for a host that is gone or now presents another key). ssh's own failures are classified:
10 the host key did not verify (changed, or not yet known), 11 authentication failed, 255 the host
is not reachable (12 with `classified=True`: callers that want a distinct code; retry_when probes
keep 255, which keeps a waiting task asleep and counts towards the network-outage alert).
"""
from __future__ import annotations

import re
import subprocess

from . import locks

UNREACHABLE_RC = 255   # what ssh exits with; netwait counts it as the network, the task sleeps on
UNKNOWN_RC = 2         # a resource no map names: the probe is broken and wakes the task
HOST_KEY_RC = 10       # the host key did not verify: wakes a waiting task, waiting will not fix it
AUTH_RC = 11           # authentication failed: likewise
CLASSIFIED_UNREACHABLE_RC = 12
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
       "-o", "UpdateHostKeys=no", "-o", "LogLevel=ERROR",
       "-o", "ControlMaster=no", "-o", "ControlPath=none"]
_HOST_KEY = re.compile(r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|"
                       r"host key for .* has changed|No \S+ host key is known for", re.I)
_AUTH = re.compile(r"Permission denied \(|Too many authentication failures|"
                   r"No more authentication methods|Authentication failed", re.I)


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


def classify(rc: int, out: str, classified: bool = False) -> int:
    """The probe's exit for ssh's exit `rc` and output: ssh's own failure (255) becomes 10 (host
    key), 11 (auth) or unreachable (255, or 12 when `classified`); any other exit is kept."""
    if rc != UNREACHABLE_RC:
        return rc
    if _HOST_KEY.search(out or ""):
        return HOST_KEY_RC
    if _AUTH.search(out or ""):
        return AUTH_RC
    return CLASSIFIED_UNREACHABLE_RC if classified else UNREACHABLE_RC


def preflight(host: str, timeout: float = 30.0, classified: bool = False) -> tuple[int, str]:
    """(exit code, ssh's output) of `ssh <host> true`: 0 reachable, 10 host key not verified,
    11 auth failed, 255 not reachable (12 when `classified`)."""
    try:
        r = subprocess.run([*SSH, host, "true"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return classify(UNREACHABLE_RC, "", classified), \
            f"ssh: connect to host {host}: timed out after {timeout:g} s"
    except OSError as e:
        return classify(UNREACHABLE_RC, "", classified), f"ssh: {e}"
    out = (r.stderr or r.stdout or "").strip()
    return classify(r.returncode, out, classified), out
