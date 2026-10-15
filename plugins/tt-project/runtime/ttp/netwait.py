# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One alert per network outage, model-free (Daemon.check_unreachable).

When this machine loses its network (or the VPN or network sign-in its hosts need), every probe that reaches another host over ssh
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

Hosts are named by the ssh or machines-list alias the probe used, never by the address ssh resolved
(a resolved address follows in parentheses only when it is not loopback). When every named host's
route (`ssh -G <alias>`) ends at loopback, directly or through its ProxyJump or ProxyCommand, the
host is reached through a tunnel from the user's workstation that reconnects by itself: the alert
says so, gives no VPN advice, and is a low feed line (no page, not in the top section) until the
outage is `waiting.tunnel_outage_page_after_s` old (3600 s), when it is raised once to normal under
the same key, so it stays one issue that clears the same way.

A probe's 255 counts as unreachable only when it is ssh's own: every host the probe names is checked
with `ssh -o BatchMode=yes -o ConnectTimeout=10 <alias> true` (confirm). A probe that names no host, or
whose host answers, failed its own remote command (e.g. lost quotes and `|| exit 255`): the daemon
treats it as a broken probe against its task, never as an outage.
The daemon checks the hosts of every probe that exited 255 in one tick together (reach), so an
outage holds its tick for one ConnectTimeout, not one per probe; each host is asked once a minute.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import timefmt

KEY = "network:unreachable"
STATE_KEY = "network_outage"   # kv: {since, last, alerted, tasks, hosts, tunnel, raised} of the open outage
GRACE_S = 900.0
DEBOUNCE_S = 600.0
TUNNEL_PAGE_S = 3600.0         # a tunnel route's outage stays a low feed line this long
UNREACHABLE_RC = 255           # ssh: the host could not be reached
HOSTS_MAX = 4
_OUT_HOST = re.compile(r"(?:connect to host|resolve hostname|[Cc]onnection to|connection to host) "
                       r"([A-Za-z0-9_.\-]+)")
_SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")   # ssh options that take a value
_SUBST = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=)?(?:\$\(|`)+")   # `x=$(`, `$(` or a backtick before ssh
REACH_THREADS = 32             # hosts checked at once in one reach() pass
CONFIRM_TIMEOUT_S = 10         # ConnectTimeout of the check that a probe's host really is unreachable


def settings(cfg: dict) -> tuple[float, float]:
    """(grace_s, debounce_s) from the `waiting` config; grace 0 turns the alert off."""
    w = cfg.get("waiting") or {}
    grace = w.get("unreachable_alert_s", GRACE_S)
    debounce = w.get("unreachable_debounce_s", DEBOUNCE_S)
    return float(GRACE_S if grace is None else grace), float(DEBOUNCE_S if debounce is None else debounce)


def tunnel_page_after(cfg: dict) -> float:
    """How old a tunnel route's outage gets before its low line is raised to normal (0: at once)."""
    v = (cfg.get("waiting") or {}).get("tunnel_outage_page_after_s", TUNNEL_PAGE_S)
    return float(TUNNEL_PAGE_S if v is None else v)


def _ssh_target(words: list[str]) -> str:
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            i += 2 if len(w) == 2 and w[1] in _SSH_ARG_OPTS else 1
            continue
        return w.rpartition("@")[2].rstrip(")`;")
    return ""


def hosts(probe: str, output: str = "") -> list[str]:
    """The hosts an unreachable probe names: its ssh target or `--host` alias, else those in ssh's own
    error (which shows the address it resolved)."""
    try:
        words = shlex.split(str(probe or ""))
    except ValueError:
        words = str(probe or "").split()
    # A quoted command substitution ("$(ssh h1 cat f)") is one word: its own words count too.
    words = [x for w in words for x in ([w, *w.split()] if " " in w and ("$(" in w or "`" in w) else [w])]
    out = []
    for i, w in enumerate(words):
        if Path(_SUBST.sub("", w)).name == "ssh":
            target = _ssh_target(words[i + 1:])
        elif w == "--host" and i + 1 < len(words):
            target = words[i + 1]
        else:
            continue
        if target and target not in out:
            out.append(target)
    return out or list(dict.fromkeys(_OUT_HOST.findall(output or "")))


def answers(alias: str, env: dict | None = None) -> bool:
    """ssh reaches `alias`: anything but ssh's own 255 (or no answer at all) means the host answered."""
    if not alias or alias.startswith("-"):
        return False
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONFIRM_TIMEOUT_S}", alias, "true"],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=CONFIRM_TIMEOUT_S + 20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode != UNREACHABLE_RC


def reach(aliases: list[str], env: dict | None = None) -> dict[str, bool]:
    """answers() for each alias, all at once: in an outage the pass waits out one timeout, not one each."""
    names = list(dict.fromkeys(aliases))
    if not names:
        return {}
    with ThreadPoolExecutor(min(len(names), REACH_THREADS)) as ex:
        return dict(zip(names, ex.map(lambda h: answers(h, env), names)))


def confirm(probe: str, output: str = "", answered=None) -> str:
    """Why a probe's exit 255 is not ssh's own ("" when every host it names is unreachable): it names
    no ssh host, or one answers, so its own remote command failed. `answered(alias)` checks one host."""
    names = hosts(probe, output)[:HOSTS_MAX]
    if not names:
        return "exit 255 from a probe that names no ssh host"
    with ThreadPoolExecutor(len(names)) as ex:   # at once: in an outage each waits out its timeout
        up = [h for h, ok in zip(names, ex.map(answered or answers, names)) if ok]
    if up:
        return f"exit 255 though ssh reaches {', '.join(up)}: its remote command failed"
    return ""


def loopback(host: str) -> bool:
    """127.0.0.0/8, ::1 or localhost."""
    h = str(host or "").strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _ssh_config(alias: str) -> dict:
    """`ssh -G <alias>`: the options ssh would use for it ({} when ssh cannot say)."""
    try:
        r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    cfg = {}
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    return cfg


def _hop_host(hop: str) -> str:
    """The host of a ProxyJump hop ([user@]host[:port] or ssh://[user@]host[:port])."""
    hop = hop.strip().removeprefix("ssh://").rpartition("@")[2]
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    return hop.rpartition(":")[0] if hop.count(":") == 1 else hop


def route(alias: str, depth: int = 0) -> dict:
    """{"hostname": what ssh resolves the alias to, "tunnel": whether the route ends at loopback}.
    A tunnel: the HostName is loopback, or the last ProxyJump hop or the ProxyCommand's own target
    is (followed through other aliases a few hops deep)."""
    cfg = _ssh_config(alias) if depth < 4 else {}
    name = cfg.get("hostname") or alias
    if loopback(name) or loopback(alias):
        return {"hostname": name, "tunnel": True}
    jump = cfg.get("proxyjump") or ""
    if jump and jump.lower() != "none":
        last = _hop_host(jump.split(",")[-1])
        return {"hostname": name, "tunnel": bool(last) and route(last, depth + 1)["tunnel"]}
    cmd = cfg.get("proxycommand") or ""
    if cmd and cmd.lower() != "none":
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for i, w in enumerate(words):
            if Path(w).name == "ssh":
                target = _ssh_target(words[i + 1:])
                return {"hostname": name, "tunnel": bool(target) and route(target, depth + 1)["tunnel"]}
        # nc/socat/connect: a loopback address or host:port among its words
        tunnel = any(loopback(x) or loopback(_hop_host(x))
                     for w in words for x in {*re.split(r"[,=]", w), *re.split(r"[,=:]", w)} if x)
        return {"hostname": name, "tunnel": tunnel}
    return {"hostname": name, "tunnel": False}


def shown(alias: str, rt: dict) -> str:
    """The alias, with what it resolves to in parentheses when that differs and is not loopback."""
    name = str(rt.get("hostname") or "")
    return f"{alias} ({name})" if name and name != alias and not loopback(name) else alias


def describe(names: list[str]) -> tuple[list[str], bool]:
    """(the hosts as the alert shows them, whether every one is reached through a tunnel). A bare
    loopback address (ssh's error names only what it resolved) is never shown as a host."""
    routes = [(h, route(h)) for h in names]
    return ([shown(h, rt) for h, rt in routes if not loopback(h)],
            bool(routes) and all(rt["tunnel"] for _, rt in routes))


def step(st: dict | None, blocking: bool, unreachable: int, now: float, grace: float, debounce: float,
         healing: bool = False, page_after: float = TUNNEL_PAGE_S) -> tuple[dict, str | None]:
    """The outage state after one check, and "alert", "raise", "clear" or None.

    `blocking`: every task that could start waits on an unreachable host and no worker runs.
    `unreachable`: how many queued tasks' latest probe says unreachable (keeps an alerted outage).
    "raise": an outage alerted low on a tunnel route (st["tunnel"], set by the caller) has lasted
    `page_after`: raise it once to normal."""
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
        if st.get("alerted") and st.get("tunnel") and not st.get("raised") and now - since >= page_after:
            st["raised"] = now
            return st, "raise"
        return st, None
    if st and now - float(st.get("last") or 0) > debounce:
        return {}, ("clear" if st.get("alerted") else None)
    return st, None


def text(where, since: float, n_tasks: int, names: list[str], tunnel: bool = False, now: float | None = None) -> str:
    """The alert: what cannot be reached, since when (home zone), how many tasks wait, what to do.
    `tunnel`: every host is reached through a tunnel from the user's workstation (no VPN advice);
    `now` (the raise to normal) adds how long it has been down."""
    shown_ = ", ".join(names[:HOSTS_MAX]) + (f" (+{len(names) - HOSTS_MAX} more)" if len(names) > HOSTS_MAX else "")
    what = f"{shown_} cannot" if names else "The hosts this project's work needs cannot"
    them, need = ("it", "it needs") if len(names) == 1 else ("them", "they need")
    held = f"{n_tasks} queued task(s) that need {them} are held; the rest of the project keeps running."
    if tunnel:
        down = f", down {(now - since) / 3600:.1f} h" if now is not None else ""
        return (f"Tunnel: {what} be reached since {timefmt.long(since, where)}{down}: the tunnel from the "
                f"user's workstation that reaches {them} is down. It reconnects by itself once the "
                f"workstation is back; the held work then resumes. {held}")
    return (f"Network: {what} be reached from the machine running this project since "
            f"{timefmt.long(since, where)}. Check that machine's network, and its VPN or network sign-in "
            f"if {need} one; the held work resumes by itself once {them} answer{'s' if len(names) == 1 else ''}. "
            f"{held}")
