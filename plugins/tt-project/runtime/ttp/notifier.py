# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Desktop notifications for the machine the user sits at, with no account or setup.

One small agent per workstation watches every project in this machine's registry — local ones by
reading their database, remote ones with one ssh call per minute — and shows each new alert at
or above the severity floor as a native notification (macOS Notification Center, or notify-send
on a Linux desktop). It never marks anything read; chats and the web app keep their own cursors.
A remote host whose ssh polls keep failing gets one notification once it has been down for
HOST_DOWN_AFTER_S, and one more when it answers again: while it is down its projects cannot page.
So does a project whose daemon has not beaten for DAEMON_DOWN_AFTER_S while a service should keep it
running: its alerts would otherwise stop with no sign, as `ttp alerts` on an answering host is empty.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import headline, sshcmd, timefmt
from .db import DB, SEVERITY_RANK
from .project import HOME_DIR, Project, durable_write, hostname, load_registry, write_json

STATE = HOME_DIR / "notifier-state.json"
CONF = HOME_DIR / "notifier.json"
HOSTS = "_hosts"   # state key for remote host reachability; no project name starts with '_'
HOST_DOWN_AFTER_S = 20 * 60   # notifier.json `host_down_after_s` overrides
HOST_POLL_GAP_S = 5 * 60   # polls further apart (sleep, notifier stopped) do not continue a failing streak
DAEMONS = "_daemons"   # state key for projects whose daemon is down
DAEMON_DOWN_AFTER_S = 3 * 3600   # notifier.json `daemon_down_after_s` overrides (floor 2 * HEARTBEAT_STALE_S)
NO_HEALTH = "_no_health"   # state key: host -> when its older ttp rejected `alerts --health`
HEALTH_RETRY_S = 3600   # how often such a host is asked with --health again (it may have been upgraded)


def alerts_since(p: Project, after: int, floor: str = "high") -> list[dict]:
    """Broadcasts at or above the floor after `after`. Ones whose condition has since cleared are
    returned marked `cleared`, so the reader moves its cursor past them without showing them."""
    from .web import cleared
    rank = SEVERITY_RANK.get(floor, 2)
    # Filter severity in the query: a page of quieter broadcasts would otherwise stall the cursor.
    levels = [s for s, r in SEVERITY_RANK.items() if r >= rank]
    db = DB(p.state / "project.db")
    try:
        rows = db.q("SELECT id, ts, kind, severity, text, ref, channel FROM messages WHERE direction='out' AND chat IS NULL "
                    f"AND id>? AND severity IN ({','.join('?' * len(levels))}) ORDER BY id LIMIT 50",
                    (after, *levels))
        now = time.time()
        for r in rows:
            # Information (a host reboot) is never pushed as an alert; the cursor still moves past it.
            # So is a quiet alert: another project on this machine already raised the same condition.
            r["cleared"] = r["kind"] == "info" or r.pop("channel") == "quiet" or cleared(db, r, now)
            del r["ref"]
    finally:
        db.close()
    return rows


def health(p: Project, now: float | None = None) -> dict:
    """{heartbeat_age_s (None without a heartbeat), service (its kind, None without one), paused}. The age
    is taken on the project's own host, so a reader elsewhere never compares two machines' clocks."""
    from . import service
    now = time.time() if now is None else now
    try:
        age = max(0.0, now - (p.state / "heartbeat").stat().st_mtime)
    except OSError:
        age = None
    svc = service.installed(p)
    db = DB(p.state / "project.db")
    try:
        paused = bool(db.kv("paused", False))
    finally:
        db.close()
    return {"heartbeat_age_s": age, "service": svc["kind"] if svc else None, "paused": paused}


def _remote_alerts(entry: dict, name: str, after: int, floor: str, state: dict | None = None,
                   now: float | None = None) -> tuple[list[dict] | None, bool, dict | None]:
    """(the alerts, or None when the poll failed; whether ssh reached the host; the project's health,
    or None when unknown). ssh exits 255 when it cannot connect; any other failure is the remote
    command's, on a host that answers. A host whose older ttp rejects --health is asked without it,
    and with it again once an hour (kept in `state`)."""
    now = time.time() if now is None else now
    no_health = state.setdefault(NO_HEALTH, {}) if state is not None else {}
    host = _host(entry)
    try:
        sshcmd.check_host(host)
    except sshcmd.BadHost:
        return None, False, None
    ask = now - float(no_health.get(host, 0)) >= HEALTH_RETRY_S
    cmd = sshcmd.remote_cmd(entry, ["alerts", name, "--after", after, "--floor", floor, "--json"])
    for flag in (" --health", "") if ask else ("",):
        try:
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", host,
                                  cmd + flag], capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            return None, False, None
        if flag and out.returncode == 2 and "unrecognized arguments" in (out.stderr or ""):
            no_health[host] = now
            continue
        break
    if out.returncode != 0:
        return None, out.returncode != 255, None
    try:
        got = json.loads(out.stdout or "[]")
    except ValueError:
        return None, True, None
    if not isinstance(got, dict):
        return got, True, None
    if flag:
        no_health.pop(host, None)
    return got.get("alerts") or [], True, got.get("health") if isinstance(got.get("health"), dict) else None


def _host(entry: dict) -> str:
    return entry.get("ssh") or entry["host"]


def _track_hosts(state: dict, reached: dict[str, bool], now: float, down_after_s: float,
                 max_gap_s: float = HOST_POLL_GAP_S, started: float | None = None) -> None:
    """One notification once a host has failed every poll for `down_after_s`, one when it answers
    again, nothing in between. Kept in the notifier's state, so a restart neither forgets nor re-alarms.
    A gap over `max_gap_s` from the last failed poll's end to this poll's start (a sleeping laptop)
    starts the streak anew; the poll's own time (slow ssh to many hosts) is no gap."""
    started = now if started is None else started
    hosts = state.setdefault(HOSTS, {})
    if reached:   # an empty registry read must not drop an alarmed host (it would alarm twice)
        for host in [h for h in hosts if h not in reached]:   # no project on it any more
            del hosts[host]
    for host, ok in sorted(reached.items()):
        h = hosts.setdefault(host, {})
        if ok:
            if h.get("alarmed"):
                show(f"tt-project · {host}", f"{host} answers again; its projects can page again")
            hosts[host] = {"ok": now}
            continue
        if not h.get("alarmed") and started - h.get("last_fail", started) > max_gap_s:
            h.pop("failing_since", None)
        since = h.setdefault("failing_since", now)
        h["last_fail"] = now
        if not h.get("alarmed") and now - since >= down_after_s:
            zone = timefmt.ambient()
            show(f"tt-project · {host}",
                 f"{host} unreachable since {timefmt.short(since, zone, now)}; its projects cannot page")
            h["alarmed"] = True


def _track_daemons(state: dict, health: dict[str, dict | None], hosts_of: dict[str, str | None], now: float,
                   down_after_s: float, host_alarmed: set[str] = frozenset(), names: set[str] | None = None) -> None:
    """One notification once a project's daemon has not beaten for `down_after_s` while a service should
    keep it running, one when it beats again, nothing in between; kept in the state across restarts.
    `health` holds this poll's answers (None: unknown, nothing changes); `hosts_of` names each remote
    project's host (None: this machine); `host_alarmed` the hosts alarmed as unreachable before this
    poll: their alarm (or recovery notice) already speaks for the projects on them."""
    daemons = state.setdefault(DAEMONS, {})
    if names:   # an empty registry read must not drop an alarmed project (it would alarm twice)
        for name in [n for n in daemons if n not in names]:
            del daemons[name]
    zone = None
    for name, h in sorted(health.items()):
        if not isinstance(h, dict):
            continue
        host = hosts_of.get(name)
        age = h.get("heartbeat_age_s")
        down = bool(h.get("service")) and isinstance(age, (int, float)) and age >= down_after_s
        was = daemons.get(name)
        if not down:
            if was:
                del daemons[name]
                # a stopped service is no recovery; nor is one the host's recovery notice just covered
                if h.get("service") and host not in host_alarmed:
                    show(f"tt-project · {name}", f"{name} daemon runs again; it can page again")
            continue
        if was:
            continue
        if host in host_alarmed and state.get(HOSTS, {}).get(host, {}).get("alarmed"):
            continue
        zone = zone or timefmt.ambient()
        since = now - age
        where = f"its host {host} answers. `ttp restart {name}` on that host." if host else \
            f"`ttp restart {name}` on this machine."
        show(f"tt-project · {name}", f"{name} daemon down since {timefmt.short(since, zone, now)} "
             f"(no heartbeat for {age / 3600:.1f} h); {where}")
        daemons[name] = {"alarmed": now, "since": since}


def show(title: str, body: str, url: str | None = None) -> None:
    body = headline.clip(body, 240)
    if sys.platform == "darwin":
        tn = shutil.which("terminal-notifier")
        if tn:
            args = [tn, "-title", title, "-message", body, "-sound", "default", "-group", title]
            if url:
                args += ["-open", url]
            subprocess.run(args, capture_output=True, timeout=10)
            return
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    elif shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        subprocess.run(["notify-send", "-a", "tt-project", title, body], capture_output=True, timeout=10)


def run_once(state: dict, floor: str, down_after_s: float = HOST_DOWN_AFTER_S, now: float | None = None,
             max_gap_s: float = HOST_POLL_GAP_S, daemon_down_after_s: float = DAEMON_DOWN_AFTER_S) -> dict:
    started = time.time() if now is None else now
    reached: dict[str, bool] = {}
    healths: dict[str, dict | None] = {}
    hosts_of: dict[str, str | None] = {}
    projects = load_registry().get("projects", {})
    for name, entry in sorted(projects.items()):
        after = int(state.get(name, -1))
        local = entry.get("host") in (None, hostname())
        if local:
            p = Project(entry["dir"])
            if not (p.state / "project.db").exists():
                continue
            hosts_of[name] = None
            try:
                healths[name] = health(p)
            except Exception:   # unknown health changes nothing; its alerts still go out
                healths[name] = None
            if after < 0:   # first sighting: start from now, do not replay history
                db = DB(p.state / "project.db")
                state[name] = int(db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"])
                db.close()
                continue
            rows = alerts_since(p, after, floor)
        else:
            rows, ok, healths[name] = _remote_alerts(entry, name, max(after, 0), floor, state, started)
            hosts_of[name] = _host(entry)
            reached[_host(entry)] = reached.get(_host(entry), False) or ok
            if rows is None:
                continue
            if after < 0:
                state[name] = max([r["id"] for r in rows] + [0])
                continue
        for r in rows:
            if not r.get("cleared"):
                show(f"tt-project · {name}", r["text"])
            state[name] = max(int(state.get(name, 0)), int(r["id"]))
    if reached:   # hosts no project lives on any more need no --health memory
        no_health = state.get(NO_HEALTH, {})
        for host in [h for h in no_health if h not in reached]:
            del no_health[host]
    host_alarmed = {h for h, v in state.get(HOSTS, {}).items() if v.get("alarmed")}
    end = time.time() if now is None else now
    _track_hosts(state, reached, end, down_after_s, max_gap_s, started)
    _track_daemons(state, healths, hosts_of, end, daemon_down_after_s, host_alarmed, set(projects))
    return state


def main() -> int:
    from .daemon import HEARTBEAT_STALE_S
    conf = {"min_severity": "high", "interval_s": 60, "host_down_after_s": HOST_DOWN_AFTER_S,
            "daemon_down_after_s": DAEMON_DOWN_AFTER_S}
    try:
        conf.update(json.loads(CONF.read_text()))
    except (OSError, ValueError):
        pass
    daemon_down_after_s = max(2 * HEARTBEAT_STALE_S, float(conf["daemon_down_after_s"]))
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    while True:
        try:
            state = run_once(state, conf["min_severity"], float(conf["host_down_after_s"]),
                             max_gap_s=max(HOST_POLL_GAP_S, 3 * int(conf["interval_s"])),
                             daemon_down_after_s=daemon_down_after_s)
            write_json(STATE, state)
        except Exception as e:  # keep notifying other projects even if one fails
            print(f"notifier: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(max(15, int(conf["interval_s"])))


def install() -> str:
    """Background agent for this user on this machine (launchd or systemd user unit)."""
    runtime = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "ttp.notifier"]
    from .providers.base import service_path
    env = {"PYTHONPATH": runtime, "PATH": service_path(), "HOME": str(Path.home())}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib
        label = "com.tt-project.notifier"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        durable_write(plist, plistlib.dumps({
            "Label": label, "ProgramArguments": argv, "WorkingDirectory": str(HOME_DIR),
            "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
            "ProcessType": "Background", "StandardOutPath": str(HOME_DIR / "notifier.log"),
            "StandardErrorPath": str(HOME_DIR / "notifier.log")}))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
        return "installed (launchd)" if r.returncode == 0 else f"launchd failed: {r.stderr.strip()[:200]}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    envs = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    durable_write(
        unit_dir / "tt-project-notifier.service",
        f"[Unit]\nDescription=tt-project desktop notifier\n\n[Service]\nWorkingDirectory={HOME_DIR}\n"
        f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n{envs}\nRestart=always\nRestartSec=15\n\n"
        f"[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "tt-project-notifier.service"],
                       capture_output=True, text=True)
    return "installed (systemd user unit)" if r.returncode == 0 else f"systemd failed: {r.stderr.strip()[:200]}"


if __name__ == "__main__":
    sys.exit(main())
