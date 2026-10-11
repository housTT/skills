# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One alert per network outage, model-free (Daemon.check_unreachable).

When this machine loses its VPN or network sign-in, every probe that reaches another host over ssh
exits 255 and the tasks behind them sleep on. Only the user can sign back in, so once every task that
could otherwise start has waited on such a probe for `waiting.unreachable_alert_s` (900 s; 0: off),
with no worker running and nothing else startable, one keyed normal alert (KEY) says so. It is
tracked like a high alert (alerts.USER_BLOCKERS): shown in the top section of the web app and
`ttp status`, sent to the chats once, never repeated while the outage lasts.

Neutral holds (dependencies, start_after or a waiting task's timer, a user-paused resource, a probe
that says plain "not yet") neither count nor prevent it; a task that could start, or one held by
anything else (budget gate, logout, disk guard, busy lock), means the project is not blocked on the
network and nothing is alerted. A heal check still trying its fix (unhealthy, not escalated, no
self-fix task open) delays the alert by up to one more grace: a tunnel or route fix may bring the
hosts back.

The outage lasts while any queued task's latest probe says unreachable. It ends once none has for
`waiting.unreachable_debounce_s` (600 s, longer than a probe interval): a brief reconnect stays the
same outage. Then the alert clears with one low feed line (alerts.CLEARED_SEVERITY). No event is
raised: the coordinator has nothing to do about it.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import timefmt

KEY = "network:unreachable"
STATE_KEY = "network_outage"   # kv: {since, last, alerted, tasks, hosts} of the open outage
GRACE_S = 900.0
DEBOUNCE_S = 600.0
UNREACHABLE_RC = 255           # ssh: the host could not be reached
HOSTS_MAX = 4
_OUT_HOST = re.compile(r"(?:connect to host|resolve hostname|[Cc]onnection to|connection to host) "
                       r"([A-Za-z0-9_.\-]+)")
_SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")   # ssh options that take a value


def settings(cfg: dict) -> tuple[float, float]:
    """(grace_s, debounce_s) from the `waiting` config; grace 0 turns the alert off."""
    w = cfg.get("waiting") or {}
    grace = w.get("unreachable_alert_s", GRACE_S)
    debounce = w.get("unreachable_debounce_s", DEBOUNCE_S)
    return float(GRACE_S if grace is None else grace), float(DEBOUNCE_S if debounce is None else debounce)


def _ssh_target(words: list[str]) -> str:
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            i += 2 if len(w) == 2 and w[1] in _SSH_ARG_OPTS else 1
            continue
        return w.rpartition("@")[2]
    return ""


def hosts(probe: str, output: str = "") -> list[str]:
    """The hosts an unreachable probe names: from ssh's own error, else the ssh target or `--host`."""
    found = _OUT_HOST.findall(output or "")
    if found:
        return list(dict.fromkeys(found))
    try:
        words = shlex.split(str(probe or ""))
    except ValueError:
        words = str(probe or "").split()
    out = []
    for i, w in enumerate(words):
        if Path(w).name == "ssh":
            target = _ssh_target(words[i + 1:])
        elif w == "--host" and i + 1 < len(words):
            target = words[i + 1]
        else:
            continue
        if target and target not in out:
            out.append(target)
    return out


def step(st: dict | None, blocking: bool, unreachable: int, now: float, grace: float, debounce: float,
         healing: bool = False) -> tuple[dict, str | None]:
    """The outage state after one check, and "alert", "clear" or None.

    `blocking`: every task that could start waits on an unreachable host and no worker runs.
    `unreachable`: how many queued tasks' latest probe says unreachable (keeps an alerted outage)."""
    st = dict(st or {})
    if blocking or (st.get("alerted") and unreachable):
        if not st or now - float(st.get("last") or 0) > debounce:
            st = {"since": now}    # a new outage: the last one ended more than the debounce ago
        st["last"] = now
        since = float(st["since"])
        if not st.get("alerted") and blocking and grace > 0 and now - since >= grace \
                and not (healing and now - since < 2 * grace):
            st["alerted"] = now
            return st, "alert"
        return st, None
    if st and now - float(st.get("last") or 0) > debounce:
        return {}, ("clear" if st.get("alerted") else None)
    return st, None


def text(where, since: float, n_tasks: int, names: list[str]) -> str:
    """The alert: what cannot be reached, since when (home zone), how many tasks wait, what to do."""
    shown = ", ".join(names[:HOSTS_MAX]) + (f" (+{len(names) - HOSTS_MAX} more)" if len(names) > HOSTS_MAX else "")
    what = f"{shown} cannot" if names else "The hosts this project's work needs cannot"
    return (f"Network: {what} be reached from the machine running this project since "
            f"{timefmt.long(since, where)}; {n_tasks} queued task(s) wait on it and nothing else can start. "
            f"Reconnect the VPN or network sign-in on that machine; the waiting work resumes by itself "
            f"once the hosts answer.")
