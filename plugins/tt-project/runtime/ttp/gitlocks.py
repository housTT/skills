# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Stale `.git/*.lock` files in the harness repository, moved aside model-free.

A git command killed mid-write (a crash, a reboot, a timeout) leaves its lock file behind, and every
later harness commit then fails until someone removes it. A lock counts as stale only when it is
older than `harness_git.stale_lock_s` (600 s; 0: off; the old `watchdog.stale_git_lock_s` is read
when only it is set), far past any harness git command's timeout, and nothing holds it: no process
has the file open (Linux) and no git process of this user runs in or names the repository. A git
process whose working directory cannot be read counts as holding it. Stale locks are moved to
`.git/ttp-stale-locks/<stamp>/`, never deleted. Project.clear_stale_git_locks runs this before each
harness commit and in the daemon's harness sweep. Only the harness's own repository is ever touched,
never a user's checkout or a task's worktree.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

STALE_S = 600.0
KEEP_DIR = "ttp-stale-locks"
_SKIP_DIRS = {"objects", "lfs", KEEP_DIR}


def settings(cfg: dict) -> float:
    """The age in seconds at which a lock counts as stale (0: off) from project.json's settings."""
    cfg = cfg or {}
    v = (cfg.get("harness_git") or {}).get("stale_lock_s")
    if v is None:
        v = (cfg.get("watchdog") or {}).get("stale_git_lock_s")
    try:
        return max(float(STALE_S if v is None else v), 0.0)
    except (TypeError, ValueError):
        return STALE_S


def git_dir(repo: Path) -> Path | None:
    """The repository's git directory: `.git` itself, or where a `.git` file points."""
    dot = Path(repo) / ".git"
    if dot.is_dir():
        return dot
    try:
        line = dot.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line[len("gitdir:"):].strip())
    return target if target.is_absolute() else (Path(repo) / target).resolve()


def candidates(gdir: Path, now: float, min_age_s: float) -> list[Path]:
    """Lock files under the git directory older than min_age_s."""
    out = []
    for top, dirs, files in os.walk(gdir):
        if Path(top) == gdir:
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".lock"):
                continue
            path = Path(top) / f
            try:
                if now - path.lstat().st_mtime >= min_age_s:
                    out.append(path)
            except OSError:
                continue
    return sorted(out)


def _own_processes() -> list[tuple[int, str]]:
    """(pid, command line) of this user's processes; ps -U works on Linux and macOS."""
    r = subprocess.run(["ps", "-o", "pid=,args=", "-U", str(os.getuid())], capture_output=True, text=True,
                       timeout=20)
    out = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != os.getpid():
            out.append((int(pid), args.strip()))
    return out


def _is_git(args: str) -> bool:
    name = Path(args.split(" ", 1)[0]).name if args else ""
    return name == "git" or name.startswith("git-")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return next((Path(x[1:]) for x in r.stdout.splitlines() if x.startswith("n/")), None)


def _open_fds(pid: int) -> set[str]:
    fd_dir = Path(f"/proc/{pid}/fd")
    out = set()
    try:
        for fd in fd_dir.iterdir():
            try:
                out.add(os.readlink(fd))
            except OSError:
                continue
    except OSError:
        pass
    return out


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def holders(repo: Path, gdir: Path, locks: list[Path], procs: list[tuple[int, str]] | None = None) -> list[int]:
    """Pids that may hold a lock of this repository: a process with one of the lock files open, or a
    git process running in or naming the repository (or whose working directory is unreadable)."""
    roots = [Path(repo).resolve(), Path(gdir).resolve()]
    lock_names = {str(p) for p in locks} | {str(p.resolve()) for p in locks if p.exists()}
    out = []
    for pid, args in (_own_processes() if procs is None else procs):
        if lock_names & _open_fds(pid):
            out.append(pid)
            continue
        if not _is_git(args):
            continue
        if any(str(r) in args for r in roots):
            out.append(pid)
            continue
        cwd = _cwd(pid)
        if cwd is None and not _alive(pid):
            continue   # ended since ps listed it
        if cwd is None or _within(cwd.resolve(), roots):
            out.append(pid)
    return out


def keep_dir(gdir: Path, now: float) -> Path:
    """Where clear() moves the stale locks it finds at `now`."""
    return Path(gdir) / KEEP_DIR / time.strftime("%Y%m%dT%H%M%S", time.localtime(now))


def clear(repo: Path, min_age_s: float = STALE_S, now: float | None = None,
          procs: list[tuple[int, str]] | None = None) -> tuple[list[Path], list[int]]:
    """Move the repository's stale lock files aside to keep_dir(): (moved, pids that kept them), the
    moved ones by their old paths. Nothing is moved while any process may hold one."""
    if min_age_s <= 0:
        return [], []
    gdir = git_dir(Path(repo))
    if gdir is None or not gdir.is_dir():
        return [], []
    now = time.time() if now is None else now
    locks = candidates(gdir, now, min_age_s)
    if not locks:
        return [], []
    held = holders(Path(repo), gdir, locks, procs)
    if held:
        return [], held
    dest = keep_dir(gdir, now)
    moved = []
    for path in locks:
        try:
            if now - path.lstat().st_mtime < min_age_s:   # written again since: a live holder
                continue
            to = dest / path.relative_to(gdir)
            to.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, to)
            moved.append(path)
        except OSError:
            continue
    return moved, []
