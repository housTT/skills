"""setup-box bare-metal route: `box.py bare check <alias>`, read-only readiness of a box.

One ssh call reads free disk under the box's workdir root and whether the tt-device-mcp broker is
installed and its service active; locally, `ttp machines list --json` gives the box's tags, note
and disk guard, and `ttp lock --probe <alias>-device` says whether the device is free.

Exit 0 ready, 1 a problem to fix (disk below the guard, broker installed but not active), 75 the
device lock is busy or paused (wait; never override it), 255 ssh failed. Usable as a retry_when.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import List, Optional

import box

DEFAULT_MIN_FREE_GB = 150.0  # the harness's own disk.min_free_gb default
BROKER_CMD = "tt-device-mcp"
BROKER_SERVICE = "tt-device-broker"
_PLAIN = re.compile(r"[A-Za-z0-9_./${}~-]+")


def _remote_root(root: str) -> str:
    """`root` as a remote shell word: ~ and $VARS expand, anything else is quoted."""
    if root.startswith("~"):
        root = "$HOME" + root[1:]
    return f'"{root}"' if _PLAIN.fullmatch(root) else shlex.quote(root)


def probe_script(root: str, service: str = BROKER_SERVICE) -> str:
    """Read-only shell run on the box; prints key=value lines."""
    return (
        f"d={_remote_root(root)}; "
        'while [ ! -d "$d" ] && [ "$d" != / ]; do d=$(dirname "$d"); done; '
        'echo "df_path=$d"; '
        'echo "df_kb=$(df -Pk "$d" 2>/dev/null | awk \'NR==2{print $4}\')"; '
        f"if command -v {BROKER_CMD} >/dev/null 2>&1; then echo broker_cmd=yes; else echo broker_cmd=no; fi; "
        f"echo \"broker_svc=$(systemctl is-active {shlex.quote(service)} 2>/dev/null)\""
    )


def _local(argv: List[str], timeout: float = 30) -> Optional[subprocess.CompletedProcess]:
    """A local command, or None when it is not installed."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")


def machine_entry(alias: str) -> Optional[dict]:
    """The box's `ttp machines list` entry; None when ttp or the list cannot be read."""
    r = _local(["ttp", "machines", "list", "--json"])
    if r is None or r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    return doc.get(alias, {}) if isinstance(doc, dict) else None


def check(alias: str, site_entry: Optional[dict], min_free_gb: Optional[float], resource: str,
          service: str = BROKER_SERVICE) -> "tuple[int, List[str], dict]":
    """(exit code, report lines, facts) for one box."""
    lines: List[str] = []
    facts: dict = {"alias": alias}
    problems: List[str] = []

    m = machine_entry(alias)
    if m is None:
        lines.append("machines: unknown (`ttp machines list` unreadable)")
    elif not m:
        lines.append("machines: not in `ttp machines list`")
    else:
        tags = list(m.get("tags") or [])
        facts["tags"] = tags
        lines.append(f"machines: tags {', '.join(tags) or 'none'}" + (f"; note: {m['note']}" if m.get("note") else ""))
        if "serving" in tags:
            lines.append("serving: it runs a service for others; use it last, only while that service is idle")
        if "unstable" in tags:
            lines.append("unstable: tagged unstable; read its note before using it")
    if min_free_gb is None:
        got = (m or {}).get("min_free_gb")
        min_free_gb = float(got) if isinstance(got, (int, float)) and not isinstance(got, bool) else DEFAULT_MIN_FREE_GB
    facts["min_free_gb"] = min_free_gb

    root = (site_entry or {}).get("workdir_root") or "~"
    if site_entry and site_entry.get("route") != "bare":
        lines.append(f"site: route is {site_entry.get('route')!r}, not bare; see its page")
    r = box.ssh_run(alias, probe_script(root, service), timeout=60)
    facts["ssh_rc"] = r.returncode
    ssh_ok = r.returncode == 0
    if not ssh_ok:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        lines.append(f"ssh: failed (exit {r.returncode}) {tail[0]}".rstrip())
    else:
        lines.append("ssh: ok")
        kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        try:
            free_gb = int(kv.get("df_kb", "")) / 1024 ** 2
        except ValueError:
            free_gb = None
        facts.update(df_path=kv.get("df_path", ""), free_gb=free_gb)
        if free_gb is None:
            problems.append("disk")
            lines.append(f"disk: could not read free space under {root}")
        elif min_free_gb and free_gb < min_free_gb:
            problems.append("disk")
            lines.append(f"disk: {free_gb:.0f} GB free in {kv.get('df_path')}, below the guard of "
                         f"{min_free_gb:g} GB: free space or pick another box")
        else:
            lines.append(f"disk: {free_gb:.0f} GB free in {kv.get('df_path')} (guard {min_free_gb:g} GB)")
        has_cmd, svc = kv.get("broker_cmd") == "yes", kv.get("broker_svc", "").strip() or "unknown"
        facts.update(broker_cmd=has_cmd, broker_service=svc)
        if has_cmd and svc == "active":
            lines.append(f"broker: {BROKER_CMD} present, {service} active: run device work through it")
        elif has_cmd or svc == "active":
            problems.append("broker")
            lines.append(f"broker: {BROKER_CMD} {'present' if has_cmd else 'missing'}, {service} {svc}: "
                         "do not start a second broker; report it")
        else:
            lines.append(f"broker: none ({BROKER_CMD} missing, {service} {svc}); open the device only "
                         "where TT_VISIBLE_DEVICES does not mask it")

    p = _local(["ttp", "lock", "--probe", resource])
    lock = ("unknown" if p is None else "free" if p.returncode == 0
            else "busy or paused" if p.returncode == 75 else f"error (exit {p.returncode})")
    facts["lock"] = lock
    lines.append(f"lock {resource}: {lock}" + (": wait for it, never override it" if lock == "busy or paused" else ""))

    rc = 255 if not ssh_ok else 1 if problems else 75 if lock == "busy or paused" else 0
    facts["rc"] = rc
    lines.append("verdict: " + {0: "ready", 1: "not ready: " + ", ".join(problems),
                                75: "wait for the device lock", 255: "unreachable over ssh"}[rc])
    return rc, lines, facts


def cmd_bare_check(args) -> int:
    entry = None
    try:
        entry = box.box_entry(box.load_site(args.site), args.alias)
    except box.SiteError:
        pass
    rc, lines, facts = check(args.alias, entry, args.min_free_gb, args.resource or f"{args.alias}-device",
                             args.service)
    print(json.dumps(facts) if args.json else "\n".join(lines))
    return rc


def register(sub) -> None:
    bare = sub.add_parser("bare", help="bare-metal route").add_subparsers(dest="bare_cmd", required=True)
    c = bare.add_parser("check", help="read-only: ssh, free disk, broker, device lock")
    c.add_argument("alias", help="the box's ssh and `ttp machines` alias")
    c.add_argument("--min-free-gb", type=float, dest="min_free_gb",
                   help="disk guard (default: the machine's min_free_gb, else 150; 0 = off)")
    c.add_argument("--resource", help="the device lock to probe (default <alias>-device)")
    c.add_argument("--service", default=BROKER_SERVICE, help="the broker's systemd unit")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_bare_check)
