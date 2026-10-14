# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Harness tasks that run at the same time each get a working tree of their own.

A harness task works in the project's harness checkout, where the daemon reads prompts, config and
runtime live. Two at once there would sweep up each other's uncommitted edits in a commit. So a
harness task starts in the harness checkout only while no other harness task's run works there.
Otherwise it gets a git worktree of the harness repo of its own (state/harness-worktrees/t<id>,
branch ttp/harness-t<id>), and keeps it on later runs while it exists.

When its run ends the daemon lands it (land): the branch's commits are merged into the harness
checkout, and the worktree and branch are removed once nothing is left in them. A merge that
conflicts, or that git refuses over uncommitted changes there, is undone and kept for the next
pass (the daemon retries each tick, sweep), and reported to the coordinator once per branch head
(a refusal only while no harness run works in the checkout). A merge that changed runtime/ asks
the daemon to restart, as `ttp restart` would."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .project import Project, durable_write, git_fsync_args

DIR = "harness-worktrees"
IDENT = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
REPORTED_KEY = "harness_merge_reported"   # kv: task id -> the branch head a conflict was reported for


def path(p: Project, tid: int) -> Path:
    return p.state / DIR / f"t{int(tid)}"


def branch(tid: int) -> str:
    return f"ttp/harness-t{int(tid)}"


def _git(cwd: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *git_fsync_args(), *IDENT, *args], capture_output=True,
                          text=True, timeout=timeout)


def _is_repo(p: Project) -> bool:
    return (p.harness / ".git").exists() and _git(p.harness, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def dirty(cwd: Path) -> bool:
    r = _git(cwd, "status", "--porcelain")
    return r.returncode != 0 or bool(r.stdout.strip())


def checkout_busy(p: Project, tid: int) -> bool:
    """The harness checkout is in use: another harness task's run works there (it has no worktree of
    its own). Uncommitted changes alone do not count: a config edit may stay there for days."""
    for r in p.db.q("SELECT runs.task FROM runs JOIN tasks ON tasks.id=runs.task WHERE runs.status='running' "
                    "AND tasks.kind='harness' AND runs.task!=?", (int(tid),)):
        if not path(p, r["task"]).exists():
            return True
    return False


def workdir(p: Project, tid: int) -> Path:
    """Where harness task `tid` works: its worktree when it has one, else the harness checkout while
    that is free, else a new worktree of its own."""
    own = path(p, tid)
    if own.exists():
        return own
    if not _is_repo(p) or not checkout_busy(p, tid):
        return p.harness
    own.parent.mkdir(parents=True, exist_ok=True)
    have = _git(p.harness, "rev-parse", "--verify", "-q", f"refs/heads/{branch(tid)}").returncode == 0
    args = ["worktree", "add", "-q", str(own), branch(tid)] if have else \
        ["worktree", "add", "-q", "-b", branch(tid), str(own), "HEAD"]
    r = _git(p.harness, *args, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"harness worktree for #{tid}: {(r.stderr or r.stdout).strip()[-400:]}")
    return own


def land(p: Project, tid: int) -> str:
    """Merge harness task `tid`'s worktree branch into the harness checkout (call it when no run of
    the task is live); remove the worktree and branch once nothing is left in them. Returns what
    happened: '' (no worktree), 'merged', 'kept' (uncommitted changes stay in the worktree for the
    task's next run; its commits landed), 'busy' (git refused, e.g. over the checkout's own changes)
    or 'conflict'."""
    own = path(p, tid)
    if not own.exists():
        return ""
    if not _is_repo(p):
        return "busy"
    b = branch(tid)
    ahead = _git(p.harness, "rev-list", "--count", f"HEAD..{b}")
    outcome = ""
    if ahead.returncode == 0 and int(ahead.stdout.strip() or 0) > 0:
        before = _git(p.harness, "rev-parse", "HEAD").stdout.strip()
        r = _git(p.harness, "merge", "--no-edit", "-m", f"tt-project: land harness task #{tid} ({b})", b, timeout=120)
        if r.returncode != 0:
            said = (r.stdout + r.stderr).strip()
            conflicted = bool(_git(p.harness, "diff", "--name-only", "--diff-filter=U").stdout.strip())
            if conflicted:
                _git(p.harness, "merge", "--abort")
            # a refusal over files another live run is editing passes when it commits; else it needs a hand
            if conflicted or ("would be overwritten" in said and not checkout_busy(p, tid)):
                _report_conflict(p, tid, said)
                return "conflict"
            return "busy"
        changed = _git(p.harness, "diff", "--name-only", before, "HEAD").stdout.split()
        if any(f.startswith("runtime/") for f in changed):
            from . import service
            durable_write(p.state / service.RESTART_REQUEST,
                          json.dumps({"at": time.time(), "pid": os.getpid(),
                                      "why": f"harness task #{tid}'s merged commits changed runtime/"}))
        outcome = "merged"
    if dirty(own):
        return "kept"
    r = _git(p.harness, "worktree", "remove", str(own), timeout=120)
    if r.returncode != 0 and own.exists():
        return outcome or "kept"
    _git(p.harness, "branch", "-d", b)
    return outcome or "merged"


def _report_conflict(p: Project, tid: int, why: str) -> None:
    """Tell the coordinator once per branch head: the task's commits wait on the branch."""
    head = _git(p.harness, "rev-parse", branch(tid)).stdout.strip()
    seen = p.db.kv(REPORTED_KEY, {}) or {}
    if seen.get(str(tid)) == head:
        return
    with p.db.tx():
        seen[str(tid)] = head
        p.db.set_kv(REPORTED_KEY, seen)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (time.time(), "harness", "harness_merge_conflict", "normal",
                f"Harness task #{tid}'s commits on branch {branch(tid)} do not merge into the harness checkout; "
                f"they wait in {path(p, tid)}. A harness task should merge them by hand "
                f"(`git merge {branch(tid)}` in the harness) and resolve, keeping both sides' intents. "
                f"git said: {why[-400:]}", "queued", tid))


def sweep(p: Project, running: set[int]) -> dict[int, str]:
    """Land every harness worktree whose task has no live run (`running`: task ids with one); a
    worktree whose task is gone from the database is landed too. Returns task id -> outcome."""
    root = p.state / DIR
    if not root.is_dir():
        return {}
    out = {}
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and d.name.startswith("t") and d.name[1:].isdigit()):
            continue
        tid = int(d.name[1:])
        if tid in running:
            continue
        got = land(p, tid)
        if got:
            out[tid] = got
    try:
        root.rmdir()   # only when empty
    except OSError:
        pass
    return out
