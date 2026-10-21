# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Desktop notifications for the machine the user sits at, with no account or setup.

One small agent per workstation watches every project in this machine's registry — local ones by
reading their database, remote ones with one ssh call per minute — and shows each new alert at
or above the severity floor as a native notification with a sound (macOS Notification Center through
terminal-notifier or osascript, or notify-send on a Linux desktop). It never marks anything read; chats
and the web app keep their own cursors. Every delivery is recorded (DELIVERY): a failure goes to the
notifier log and raises one alert in this machine's projects until a later delivery succeeds
(Daemon.check_notifier); `ttp notifier test` sends one and tells how to fix a blocked one. The agent
logs its code version at start (RUNNING) and exits once the installed code changes, so its service
restarts it on the new code; `ttp setup` and `ttp upgrade` restart it too.
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
DELIVERY = HOME_DIR / "notifier-delivery.json"   # the last delivery's outcome, and the code last tested (refresh)
RUNNING = HOME_DIR / "notifier-running.json"   # the agent's pid and code version, written at its start
LOG = HOME_DIR / "notifier.log"
LABEL = "com.tt-project.notifier"   # the launchd agent
UNIT = "tt-project-notifier.service"   # the systemd user unit
ALERT_KEY = "notifier"   # the projects' alert while desktop delivery fails (Daemon.check_notifier)
TIMEOUT_S = 10


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


def _command(title: str, body: str, url: str | None, severity: str = "high") -> tuple[str, list[str] | None, str]:
    """(the backend, its argv or None, why there is none) for this machine. Every one plays a sound."""
    if sys.platform == "darwin":
        tn = shutil.which("terminal-notifier")
        if tn:
            # no -group: a shared group would let a later alert replace an earlier unread one
            args = [tn, "-title", title, "-message", body, "-sound", "default"]
            return "terminal-notifier", args + (["-open", url] if url else []), ""
        if not shutil.which("osascript"):
            return "osascript", None, "osascript not found on PATH"
        # text goes as argv, never into the script source: non-ASCII or quotes cannot break it
        return "osascript", ["osascript", "-e", "on run argv",
                             "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                             "-e", "end run", body, title], ""
    if sys.platform.startswith("linux"):
        if not shutil.which("notify-send"):
            return "notify-send", None, "notify-send is not installed"
        if not any(os.environ.get(v) for v in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")):
            return "notify-send", None, "no desktop session (DISPLAY, WAYLAND_DISPLAY and DBUS_SESSION_BUS_ADDRESS unset)"
        urgency = "critical" if severity == "critical" else "normal"   # critical stays until dismissed
        return "notify-send", ["notify-send", "-a", "tt-project", "-u", urgency,
                               "-h", "string:sound-name:message-new-instant", title, body], ""
    return sys.platform, None, f"no desktop notification backend on {sys.platform}"


def show(title: str, body: str, url: str | None = None, severity: str = "high") -> dict:
    """Show one notification. Returns and records (DELIVERY) {backend, ok, confirmed, error, ts}; a
    failure (non-zero exit, timeout, no backend) also goes to the notifier log. `confirmed`: the backend
    says it was shown; on macOS exit 0 only means it was handed over, a blocked app shows nothing."""
    body = headline.clip(body, 240)
    backend, args, why = _command(title, body, url, severity)
    res = {"backend": backend, "ok": False, "confirmed": False, "error": why, "ts": time.time()}
    if args:
        try:
            r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            res["error"] = f"timed out after {TIMEOUT_S}s"
        except OSError as e:
            res["error"] = f"could not run: {e}"
        else:
            if r.returncode == 0:
                res.update(ok=True, confirmed=backend == "notify-send", error="")
            else:
                res["error"] = f"exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    record(res)
    return res


def log(line: str) -> None:
    """One timestamped line in the notifier log (launchd also sends the agent's stderr there)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')} notifier: {line}\n")
    except OSError:
        print(f"notifier: {line}", file=sys.stderr, flush=True)


def delivery() -> dict:
    try:
        d = json.loads(DELIVERY.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def record(res: dict) -> None:
    """Keep the last delivery's outcome; a failure is logged and keeps when the failing began."""
    d = delivery()
    if not res["ok"]:
        log(f"{res['backend']} delivery failed: {res['error']}")
        since = d.get("failing_since") if d.get("ok") is False else None
        res = dict(res, failing_since=since or res["ts"])
    d.update(res)
    if res["ok"]:
        d.pop("failing_since", None)
    try:
        write_json(DELIVERY, d)
    except OSError as e:
        log(f"could not record the delivery: {e}")


def failing() -> dict | None:
    """The last delivery when it failed: the condition of the ALERT_KEY alert (alerts.holds)."""
    d = delivery()
    return d if d.get("ok") is False else None


def fix_steps(res: dict) -> list[str]:
    """Short, exact steps that make desktop notifications show on this machine."""
    b, err = res.get("backend"), res.get("error") or ""
    again = "Then run `ttp notifier test` again."
    if b == "osascript":
        return ["macOS shows these as from Script Editor. If you saw nothing:",
                "  1. System Settings > Notifications > Script Editor: turn on Allow notifications (Banners or Alerts).",
                "  2. Script Editor not listed: open Script Editor, run `display notification \"test\"` once, then do step 1.",
                "  3. Turn off Focus (Do Not Disturb) or allow it there.",
                "  Or `brew install terminal-notifier`: the notifier then uses it (its own entry in Notifications).",
                "  " + again]
    if b == "terminal-notifier":
        return ["If you saw nothing:",
                "  1. System Settings > Notifications > terminal-notifier: turn on Allow notifications (Banners or Alerts).",
                "  2. Turn off Focus (Do Not Disturb) or allow it there.",
                "  " + again]
    if b == "notify-send" and "not installed" in err:
        return ["Install notify-send (Debian/Ubuntu: `sudo apt install libnotify-bin`; Fedora: `sudo dnf install "
                "libnotify`). " + again]
    if b == "notify-send" and "no desktop session" in err:
        return ["This machine has no desktop session. Install the notifier on the computer you sit at "
                "(`ttp notifier install` there), and remove it here: "
                f"`systemctl --user disable --now {UNIT}`."]
    if b == "notify-send":
        return ["No notification server answered: check that a desktop session runs and Do Not Disturb is off. " + again]
    return ["This system has no desktop notifications; alerts still reach the chat and the web app."]


def test(ask=None) -> tuple[bool, list[str]]:
    """Send one test notification the way alerts go (show). (delivered, lines to print). `ask(prompt)`
    (an interactive terminal) confirms what the backend cannot: whether the user saw it."""
    res = show("tt-project", "Test notification: desktop alerts from tt-project reach this machine.")
    b = res["backend"]
    if not res["ok"]:
        return False, [f"test notification failed ({b}): {res['error']}"] + fix_steps(res)
    if res["confirmed"]:
        return True, [f"test notification delivered ({b})"]
    if ask is None:
        return True, [f"test notification sent ({b}); macOS does not say whether it was shown"] + fix_steps(res)
    if ask("Did a tt-project notification appear? [y/N] "):
        record(dict(res, confirmed=True, ts=time.time()))
        return True, [f"test notification delivered ({b}), seen by you"]
    res = dict(res, ok=False, confirmed=False, error="sent, but you did not see it", ts=time.time())
    record(res)
    return False, [f"test notification not shown ({b}): macOS took it but did not show it"] + fix_steps(res)


def code(runtime: Path) -> str:
    """'<version> (<commit>)' of a runtime folder."""
    from .release import runtime_commit, runtime_version
    return f"{runtime_version(runtime)} ({runtime_commit(runtime)})"


def own_runtime() -> Path:
    return Path(__file__).parent.parent


def installed_code() -> str:
    from .release import installed
    rt = installed() / "runtime"
    return code(rt if rt.is_dir() else own_runtime())


def agent_file() -> Path | None:
    """This user's installed notifier agent (launchd plist or systemd user unit), None without one (or
    in tests that never touch the account's own agent: TTP_TEST_NO_NOTIFIER)."""
    if os.environ.get("TTP_TEST_NO_NOTIFIER"):
        return None
    f = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist" if sys.platform == "darwin" else \
        Path.home() / ".config" / "systemd" / "user" / UNIT
    return f if f.exists() else None


def running() -> dict | None:
    """The agent's start record (RUNNING) while its process lives; None when it is not running or
    started before agents wrote one (an unknown code version)."""
    try:
        rec = json.loads(RUNNING.read_text())
        os.kill(int(rec["pid"]), 0)
    except PermissionError:
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def status_lines() -> list[str]:
    """What `ttp status` says about this machine's notifier: nothing while it runs the installed code
    and its last delivery went out."""
    if not agent_file():
        return []
    out = []
    run, want = running(), installed_code()
    if not run:
        out.append("desktop notifier: running code unknown (not running, or started by an older ttp); "
                   "`ttp notifier restart`")
    elif run.get("code") != want:
        out.append(f"desktop notifier runs ttp {run.get('code')}, not the installed {want}: `ttp notifier restart`")
    bad = failing()
    if bad:
        out.append(f"desktop notifier: last delivery failed ({bad.get('backend')}: {bad.get('error')}); "
                   "`ttp notifier test` shows the fix")
    return out


def refresh(send_test: bool, always: bool = False) -> list[str]:
    """`ttp setup`, `ttp upgrade`: restart an installed agent so it runs the installed code (`always`, or
    while it runs other code), then (`send_test`) send the test notification once per installed code
    version: none once one went out on it. Lines to print; none without an installed agent."""
    if not agent_file():
        return []
    want = installed_code()
    run = running()
    out = []
    if always or not run or run.get("code") != want:
        out.append(f"desktop notifier: {install()} (runs ttp {want})")
    if send_test:
        if delivery().get("tested") == want:
            out.append(f"desktop notifier: a test notification already went out on ttp {want}; "
                       "`ttp notifier test` sends another")
        else:
            out += ["desktop notifier: " + ln if i == 0 else ln for i, ln in enumerate(test_once(want)[1])]
    return out


def test_once(want: str | None = None, ask=None) -> tuple[bool, list[str]]:
    """The test notification, remembered per installed code version once it went out."""
    ok, lines = test(ask)
    if ok:
        d = delivery()
        d["tested"] = want or installed_code()
        try:
            write_json(DELIVERY, d)
        except OSError:
            pass
    return ok, lines


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
                show(f"tt-project · {name}", r["text"], severity=r.get("severity") or "high")
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
    from . import __version__
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
    mine = code(own_runtime())
    py = ".".join(map(str, sys.version_info[:3]))
    write_json(RUNNING, {"pid": os.getpid(), "code": mine, "version": __version__, "python": py,
                         "runtime": str(own_runtime()), "started": time.time()})
    log(f"started: ttp {mine} under Python {py} from {own_runtime()}")
    while True:
        try:
            state = run_once(state, conf["min_severity"], float(conf["host_down_after_s"]),
                             max_gap_s=max(HOST_POLL_GAP_S, 3 * int(conf["interval_s"])),
                             daemon_down_after_s=daemon_down_after_s)
            write_json(STATE, state)
        except Exception as e:  # keep notifying other projects even if one fails
            print(f"notifier: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if outdated(mine):
            log(f"installed code is now ttp {installed_code()}; exiting so the service restarts on it")
            return 0
        time.sleep(max(15, int(conf["interval_s"])))


def outdated(mine: str) -> bool:
    """This agent runs from the installed runtime's path (lib/current) and that now holds other code: an
    exit lets launchd or systemd start it again on the new code."""
    from .release import installed
    return own_runtime() == installed() / "runtime" and installed_code() != mine


def install() -> str:
    """Background agent for this user on this machine (launchd or systemd user unit), (re)started on the
    installed runtime's stable path, so a later `ttp setup` or upgrade only needs a restart."""
    from .release import installed
    runtime = installed() / "runtime"
    if not (runtime / "ttp" / "notifier.py").is_file():   # no `ttp setup` yet: this copy
        runtime = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "ttp.notifier"]
    from .providers.base import service_path
    env = {"PYTHONPATH": str(runtime), "PATH": service_path(), "HOME": str(Path.home())}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        durable_write(plist, plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "WorkingDirectory": str(HOME_DIR),
            "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
            "ProcessType": "Background", "StandardOutPath": str(LOG), "StandardErrorPath": str(LOG)}))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
        return "installed and started (launchd)" if r.returncode == 0 else f"launchd failed: {r.stderr.strip()[:200]}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    envs = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    durable_write(
        unit_dir / UNIT,
        f"[Unit]\nDescription=tt-project desktop notifier\n\n[Service]\nWorkingDirectory={HOME_DIR}\n"
        f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n{envs}\nRestart=always\nRestartSec=15\n\n"
        f"[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    for step in (["enable", UNIT], ["restart", UNIT]):   # restart: `enable --now` leaves a running one on old code
        r = subprocess.run(["systemctl", "--user", *step], capture_output=True, text=True)
        if r.returncode != 0:
            return f"systemd failed: {r.stderr.strip()[:200]}"
    return "installed and started (systemd user unit)"


if __name__ == "__main__":
    sys.exit(main())
