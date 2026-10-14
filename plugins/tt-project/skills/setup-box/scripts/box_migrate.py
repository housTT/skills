"""`box.py migrate check`: what a machine still depends on from file syncing (devsync, mutagen)
and tt-workflows. Read-only: it reports, it changes nothing. Steps: `migrate-off-sync.md`.

Exit 0 when nothing active is left, 1 when something is (commented-out cron lines and dangling
skill links count as inactive), 2 when the check could not run.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Iterable, List, Optional

SKILL_DIRS = (".claude/skills", ".agents/skills", ".cursor/skills")
HOOK_FILES = (".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json")
TTW_NAME = "tt-workflows"
DEVRUN_RE = re.compile(r"\bdev(run|lock)\.sh\b")
PATH_RE = re.compile(r"(?:~|\$HOME|/)[^\s'\"`;|&<>()]*")
SCRIPT_SUFFIXES = {"", ".sh", ".bash", ".zsh", ".py", ".mk"}
SCRIPT_NAMES = {"Makefile", "justfile", "Justfile"}
TTW_KEYS_RE = re.compile(r"^\s*(device_host|remote_root)\s*=\s*(.+?)\s*$", re.M)
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3]
OWN_TESTS_RE = re.compile(r"^tests/test_(tt_project_\w+|plugin_catalogue)\.py$")


def _expand(p: str, home: pathlib.Path) -> pathlib.Path:
    if p.startswith("$HOME"):
        p = str(home) + p[len("$HOME"):]
    elif p.startswith("~"):
        p = str(home) + p[1:]
    return pathlib.Path(os.path.normpath(p))


def ttw_root(path: pathlib.Path) -> Optional[pathlib.Path]:
    """The tt-workflows clone `path` lies in, or None.

    A clone is a directory named tt-workflows, or one holding scripts/devrun.sh next to skills/
    (a copy under another name)."""
    for d in (path, *path.parents):
        if d.name == TTW_NAME:
            return d
        try:
            if (d / "scripts" / "devrun.sh").is_file() and (d / "skills").is_dir():
                return d
        except OSError:
            pass
    return None


def _paths_in(text: str, home: pathlib.Path) -> Iterable[pathlib.Path]:
    for m in PATH_RE.finditer(text):
        if len(m.group(0)) > 1:
            yield _expand(m.group(0), home)


def _ttw_path_in(text: str, home: pathlib.Path) -> Optional[pathlib.Path]:
    for p in _paths_in(text, home):
        if ttw_root(p):
            return p
    return None


def _add(found: list, kind: str, detail: str, active: bool = True, **extra) -> None:
    found.append({"kind": kind, "active": active, "detail": detail, **extra})


def check_devsync(home: pathlib.Path, found: list) -> None:
    units = []
    agents = home / "Library" / "LaunchAgents"
    if agents.is_dir():
        units += [p for p in sorted(agents.iterdir()) if "devsync" in p.name]
    for p in units:
        _add(found, "devsync-unit", str(p))
    sd = home / ".config" / "systemd" / "user"
    if sd.is_dir():
        files = [p for p in sorted(sd.rglob("*")) if "devsync" in p.name and not p.is_dir()]
        wanted = {p.name for p in files if p.parent.name.endswith((".wants", ".requires"))}
        for p in files:
            if p.parent.name.endswith((".wants", ".requires")):
                continue
            enabled, running = p.name in wanted, _unit_active(p.name)
            if enabled or running:
                why = ", ".join(w for w, on in (("enabled", enabled), ("running", running)) if on)
                _add(found, "devsync-unit", str(p), note=why)
            else:
                _add(found, "devsync-unit", str(p), active=False, note="disabled and stopped")
    cfg = home / ".ssh" / "config"
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for n, line in enumerate(lines, 1):
        s = line.strip()
        body = s.lstrip("#").strip()
        if body.lower().startswith("include") and "devsync" in body and "ssh-hosts" in body:
            _add(found, "ssh-include", f"{cfg}:{n}: {s}", active=not s.startswith("#"))


def _unit_active(name: str) -> bool:
    """Read-only `systemctl --user is-active`; no systemctl or no answer counts as not running."""
    exe = shutil.which("systemctl")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "--user", "is-active", "--quiet", name], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def check_mutagen(found: list) -> None:
    exe = shutil.which("mutagen")
    if not exe:
        return
    # Never start a daemon just to look: with autostart off, no daemon means no live sessions.
    env = dict(os.environ, MUTAGEN_DISABLE_AUTOSTART="1")
    try:
        r = subprocess.run([exe, "sync", "list"], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        _add(found, "mutagen", f"`mutagen sync list` did not answer: {e}")
        return
    if r.returncode != 0:
        if "daemon" in (r.stderr + r.stdout).lower():
            return
        _add(found, "mutagen", f"`mutagen sync list` exit {r.returncode}: "
             f"{(r.stderr or r.stdout).strip()[:200]}")
        return
    name = None
    for line in r.stdout.splitlines():
        key, _, val = line.strip().partition(":")
        if key == "Name":
            name = val.strip()
        elif key == "Identifier":
            _add(found, "mutagen", f"sync session {name or '?'} ({val.strip()})")
            name = None


def check_skill_links(home: pathlib.Path, found: list) -> None:
    for rel in SKILL_DIRS:
        d = home / rel
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_symlink():
                continue
            target = _expand(str(p.parent / os.readlink(p)), home)
            if ttw_root(target):
                _add(found, "skill-link", f"{p} -> {target}", active=target.exists(),
                     note="" if target.exists() else "dangling")


def _commands(node, where=()):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "command" and isinstance(v, str):
                yield where, v
            else:
                yield from _commands(v, where + (k,) if isinstance(v, (dict, list)) else where)
    elif isinstance(node, list):
        for v in node:
            yield from _commands(v, where)


def check_hooks(home: pathlib.Path, found: list) -> None:
    for rel in HOOK_FILES:
        f = home / rel
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            _add(found, "hook", f"{f}: unreadable ({e}); check its hooks by hand")
            continue
        for where, cmd in _commands(data.get("hooks", {}) if isinstance(data, dict) else {}):
            p = _ttw_path_in(cmd, home)
            if not p:
                continue
            event = next((w for w in where if not w.isdigit()), "?")
            _add(found, "hook", f"{f} {event}: {cmd}",
                 note="" if p.exists() else "script missing: errors on every matching tool call")


def check_cron(home: pathlib.Path, found: list) -> None:
    exe = shutil.which("crontab")
    if not exe:
        return
    try:
        r = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0:
        return
    for n, line in enumerate(r.stdout.splitlines(), 1):
        s = line.strip()
        if s and _ttw_path_in(s, home):
            _add(found, "cron", f"crontab line {n}: {s[:160]}", active=not s.startswith("#"))


def _git(repo: pathlib.Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in (0, 1) else None


def check_repo(repo: pathlib.Path, found: list) -> None:
    if not repo.is_dir() or ttw_root(repo.resolve()):
        return
    tomls = [repo / "ttw.toml"]
    listed = _git(repo, "ls-files", "--", "ttw.toml", "*/ttw.toml")
    tomls += [repo / p for p in (listed or "").splitlines()]
    for t in dict.fromkeys(tomls):
        try:
            text = t.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, val in TTW_KEYS_RE.findall(text):
            _add(found, "ttw-toml", f"{t}: {key} = {val}")
    hits = _git(repo, "grep", "-l", "-I", "-E", r"dev(run|lock)\.sh")
    if hits is None:
        hits = "\n".join(str(p.relative_to(repo)) for p in _walk_text(repo) if _mentions_devrun(p))
    for rel in hits.splitlines():
        p = (repo / rel).resolve()
        if rel and _is_script(p) and not ttw_root(p) and not _is_own(repo, rel, p):
            _add(found, "devrun-caller", str(repo / rel))


def _is_own(repo: pathlib.Path, rel: str, p: pathlib.Path) -> bool:
    """tt-project's own files (this script, its guide, its tests) name devrun.sh but call nothing."""
    rel = rel.replace(os.sep, "/")
    if p.is_relative_to(PLUGIN_ROOT) or rel.startswith("plugins/tt-project/"):
        return True
    return bool(OWN_TESTS_RE.match(rel)) and (repo / "plugins" / "tt-project").is_dir()


def _is_script(p: pathlib.Path) -> bool:
    """Scripts only: logs and run transcripts that quote devrun.sh call nothing."""
    return p.suffix in SCRIPT_SUFFIXES or p.name in SCRIPT_NAMES


def _walk_text(repo: pathlib.Path, limit: int = 20000):
    n = 0
    for dirpath, dirnames, files in os.walk(repo):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "build")]
        for f in files:
            n += 1
            if n > limit:
                return
            yield pathlib.Path(dirpath) / f


def _mentions_devrun(p: pathlib.Path) -> bool:
    try:
        if p.stat().st_size > 1_000_000:
            return False
        return bool(DEVRUN_RE.search(p.read_text(encoding="utf-8", errors="strict")))
    except (OSError, UnicodeDecodeError):
        return False


def default_repos(home: pathlib.Path) -> List[pathlib.Path]:
    """The current git checkout plus every project the tt-project registry knows on this machine."""
    repos = []
    top = _git(pathlib.Path.cwd(), "rev-parse", "--show-toplevel")
    if top and top.strip():
        repos.append(pathlib.Path(top.strip()))
    try:
        reg = json.loads((home / ".tt-project" / "registry.json").read_text(encoding="utf-8"))
        for p in (reg.get("projects") or {}).values():
            if isinstance(p, dict) and isinstance(p.get("dir"), str):
                repos.append(pathlib.Path(p["dir"]))
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(repos))


def migrate_check(home: pathlib.Path, repos: List[pathlib.Path]) -> list:
    found: list = []
    check_devsync(home, found)
    check_mutagen(found)
    check_skill_links(home, found)
    check_hooks(home, found)
    check_cron(home, found)
    for repo in repos:
        check_repo(repo, found)
    return found


KINDS = ("devsync-unit", "ssh-include", "mutagen", "skill-link", "hook", "cron", "ttw-toml",
         "devrun-caller")


def cmd_migrate_check(args) -> int:
    home = pathlib.Path(args.home or os.path.expanduser("~"))
    if not home.is_dir():
        print(f"error: no home directory {home}")
        return 2
    repos = [pathlib.Path(r).expanduser() for r in args.repo] if args.repo else default_repos(home)
    found = migrate_check(home, repos)
    active = sum(f["active"] for f in found)
    if args.json:
        print(json.dumps({"active": active, "found": found, "repos": [str(r) for r in repos]}, indent=2))
        return 1 if active else 0
    for kind in KINDS:
        items = [f for f in found if f["kind"] == kind]
        if not items:
            print(f"clear {kind}")
        for f in items:
            note = f" ({f['note']})" if f.get("note") else ""
            print(f"{'found' if f['active'] else 'inactive'} {kind}: {f['detail']}{note}")
    print(f"{active} active leftover(s); repos scanned: {', '.join(map(str, repos)) or 'none'}")
    print("report only: nothing was changed (steps: migrate-off-sync.md)")
    return 1 if active else 0


def register(sub) -> None:
    mig = sub.add_parser("migrate", help="moving off file syncing and tt-workflows")
    msub = mig.add_subparsers(dest="migrate_cmd", required=True)
    c = msub.add_parser("check", help="report what this machine still depends on (read-only)")
    c.add_argument("--home", help="home directory to inspect (default: yours)")
    c.add_argument("--repo", action="append", default=[],
                   help="project checkout to scan for ttw.toml and devrun.sh/devlock.sh callers "
                        "(repeatable; default: this checkout and the tt-project registry's projects)")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.set_defaults(func=cmd_migrate_check)
