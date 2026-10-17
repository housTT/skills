# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free hand-off lint: the daemon runs it on every code task's hand-off (Daemon._finish_worker),
so no worker has to call it.

A code task that may publish its own branch (delivery.own_push "on", or unset with draft PRs on:
project.own_creates_pr_branch) publishes it itself with `ttp push --own-branch`. A follow-up that
proposes pushing that branch while it is not on the remote leaves the task's own step to another
run. The hand-off is flagged in the digest as a task note, which also keeps the daemon from queueing
that follow-up as the next hop by itself (a hand-off with notes is the coordinator's). Upstream
notes are not work and are not linted. The remote is read only when a follow-up matches, and an
unreadable remote flags nothing."""
from __future__ import annotations

from pathlib import Path

from .project import Project, own_creates_pr_branch, own_push
from . import upstream

REMOTE_TIMEOUT_S = 30   # one ls-remote, in the daemon's tick


def proposes_push(result: dict, branch: str) -> str:
    """The title of the first follow-up of `result` (not an upstream note) that proposes pushing
    `branch`: its title or spec says "push" and names the branch or calls itself mechanical. "" if none."""
    b = branch.lower()
    for f in result.get("followups") or []:
        if not isinstance(f, dict) or upstream.is_note(f):
            continue
        t = f"{f.get('title') or ''} {f.get('spec') or ''}".lower()
        if "push" in t and (b in t or "mechanical" in t):
            return str(f.get("title") or "")[:200]
    return ""


def applies(cfg: dict) -> bool:
    """Whether a code task may publish its own branch under the project's delivery settings."""
    return own_push(cfg) == "on" or own_creates_pr_branch(cfg)


def _on_remote(p: Project, repo: Path, branch: str) -> bool | None:
    """Whether `branch` exists on the remote `ttp push --own` publishes to; None when unreadable or
    the repo has no remote."""
    from . import push
    d = p.config().get("delivery") or {}
    try:
        if push.remote_less(repo):
            return None
        remote = push.target(p, repo)[0] if str(d.get("push_branch") or "").strip() else "origin"
    except Exception:
        return None
    ls = push._git_net(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}", timeout_s=REMOTE_TIMEOUT_S)
    if ls.returncode != 0:
        return None
    return any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())


def lint(p: Project, cfg: dict, task: dict, result) -> list[str]:
    """Problems of a code task's hand-off, each one line for the digest; [] when clean or not linted."""
    branch = str(task.get("branch") or "")
    if task.get("kind") != "code" or not branch or not isinstance(result, dict) or not applies(cfg):
        return []
    title = proposes_push(result, branch)
    if not title or _on_remote(p, p.root, branch) is not False:
        return []
    return [f"hand-off lint: follow-up {title!r} proposes pushing {branch}, which is not on the remote. "
            f"Publishing it is the task's own step (`ttp push --own-branch --detach` from its branch): requeue "
            f"the task to do it rather than queueing that follow-up."]
