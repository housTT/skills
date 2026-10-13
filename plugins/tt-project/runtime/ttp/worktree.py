# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Isolated workspaces for code tasks: one git worktree and branch per task, under the project's
ignored folder, so parallel workers never share a working tree with each other or the user."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .db import continues_id
from .project import Project, durable_write


def _git(cwd: Path, *args: str, check: bool = True, timeout: float = 120, env: dict | None = None) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def is_git(path: Path) -> bool:
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def git_dirs(path: Path) -> list[str]:
    """The git metadata a commit in `path` writes: the worktree's own gitdir (its index, HEAD and
    locks, under <common>/worktrees/<name>) and the shared .git directory, in that order, without
    duplicates. Codex's sandbox makes the gitdir a worktree's `.git` file points to read-only, even
    inside a writable common directory, so a worker must be granted it by name."""
    try:
        out = _git(path, "rev-parse", "--git-dir", "--git-common-dir", check=False).splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    found = [(Path(path) / line).resolve() for line in out[:2] if line]
    return list(dict.fromkeys(str(d) for d in found if d.is_dir()))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


# "branch <name>", "branch: <name>" or "branch `<name>`" in the charter.
CHARTER_BRANCH = re.compile(r"\bbranch\b[:\s]+`?([A-Za-z0-9][\w./-]*[\w-])`?")


def charter_branches(p: Project) -> list[str]:
    """Branch names the charter gives as `branch <name>`, in order of appearance, without main,
    master and HEAD (a charter says those to forbid them)."""
    try:
        text = p.charter_path.read_text()
    except OSError:
        return []
    out: list[str] = []
    for name in CHARTER_BRANCH.findall(text):
        if name not in ("HEAD", "main", "master") and name not in out:
            out.append(name)
    return out


def _has(p: Project, ref: str) -> bool:
    return bool(_git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False))


def base_ref(p: Project) -> str:
    """Where code tasks branch from, the first of: `delivery.base_ref` as set; the project's working
    branch, `delivery.push_branch`, then a branch the charter names (`branch <name>`), each as
    origin/<name> when the remote has it (a local branch of that name may be behind; ensure()
    fetches first), else the local name; the remote's default branch (origin/HEAD); the
    checked-out branch."""
    return named_base(p) or _git(p.root, "rev-parse", "--abbrev-ref", "HEAD")


def named_base(p: Project) -> str:
    """base_ref without its last fallback (the checked-out branch): "" when nothing names a base."""
    d = p.config().get("delivery") or {}
    if d.get("base_ref"):
        return d["base_ref"]
    for ref in [str(d.get("push_branch") or "").strip(), *charter_branches(p)]:
        for cand in (f"origin/{ref}", ref) if ref else ():
            if _has(p, cand):
                return cand
    return _git(p.root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)


def git_timeout(p: Project, scale: float = 1.0) -> float:
    """The timeout for the git commands that make a task's workspace: `worktree.git_timeout_s`
    (default 120 s) scaled by the host's load per CPU when above 1 (a loaded host checks out
    slower) and by `scale` (a later attempt waits longer), at most `worktree.git_timeout_max_s`
    (default 900 s)."""
    w = p.config().get("worktree") or {}
    base = float(w.get("git_timeout_s") or 120)
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):
        load = 0.0
    return min(float(w.get("git_timeout_max_s") or 900), base * max(1.0, load) * scale)


def _records(p: Project) -> dict[str, dict]:
    """Path -> `git worktree list --porcelain`'s record of each worktree of the repository ({"path",
    "branch", "locked": its reason ("" when it has none) or None, "prunable": bool}). Raises when git
    fails or times out: an unknown record is no missing one."""
    out: dict[str, dict] = {}
    rec: dict | None = None
    for line in _git(p.root, "worktree", "list", "--porcelain", timeout=git_timeout(p)).splitlines() + [""]:
        key, _, val = line.partition(" ")
        if key == "worktree":
            rec = {"path": val, "branch": "", "locked": None, "prunable": False}
        elif rec is not None and key == "branch":
            rec["branch"] = val.removeprefix("refs/heads/")
        elif rec is not None and key == "locked":
            rec["locked"] = val
        elif rec is not None and key == "prunable":
            rec["prunable"] = True
        elif not line and rec is not None:
            out[rec["path"]] = rec
            rec = None
    return out


def _record_of(records: dict[str, dict], path: Path) -> dict | None:
    want = str(path.resolve()) if path.exists() else str(path)
    return records.get(want) or records.get(str(path))


def _registered(p: Project, path: Path) -> dict | None:
    """git's record of the worktree at `path` (see _records), or None when git has none there."""
    return _record_of(_records(p), path)


def _whole(p: Project, path: Path, rec: dict, branch: str) -> bool:
    """Whether the registered worktree at `path` (`rec`, from _registered) is whole and can be reused
    (True), or positively half-made, so removing it loses nothing (False): locked "initializing" by a
    `git worktree add` that never finished its checkout (no `.git` file, no index or no HEAD), so no
    task ever ran in it. Anything else is kept: a worktree with an index stays whatever it holds (a
    whole one left locked "initializing" is unlocked), an unlocked one is never removed. Raises,
    touching nothing, when a check fails or times out, or the worktree is on another branch."""
    adding = (rec["locked"] or "").startswith("initializing")
    try:
        if not (path / ".git").is_file():
            if adding:
                return False
            raise RuntimeError("it has no .git file")
        t = git_timeout(p)
        top, index = _git(path, "rev-parse", "--show-toplevel", "--git-path", "index", timeout=t).splitlines()[:2]
        if Path(top).resolve() != path.resolve():
            raise RuntimeError(f"git finds {top} there")
        if adding and not (path / index).exists():
            return False
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                             capture_output=True, text=True, timeout=t)
        if out.returncode != 0 or not out.stdout.strip():
            if adding and out.returncode == 1 and not out.stderr.strip():
                return False
            raise RuntimeError(f"its HEAD does not resolve {out.stderr.strip()[:200]}")
        if rec["branch"] and rec["branch"] != branch:
            raise RuntimeError(f"it is on branch {rec['branch']}, not {branch}")
        if adding:
            _git(p.root, "worktree", "unlock", str(path), timeout=t)
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"worktree {path} left as it is: {e}") from e


def _clear_partial(p: Project, path: Path, rec: dict | None) -> None:
    """Remove what an unfinished `git worktree add` left at `path` so it can be made again: a half-made
    registered worktree (`rec`, which _whole found half-made), or git's record of one whose directory is
    gone. A directory git does not know holding more than a `.git` file is never deleted: it is not
    ours."""
    if rec and not rec["prunable"]:
        _git(p.root, "worktree", "remove", "--force", "--force", str(path), check=False, timeout=git_timeout(p))
    _git(p.root, "worktree", "prune", check=False, timeout=git_timeout(p))
    if path.is_dir() and not _registered(p, path):
        if any(c.name != ".git" for c in path.iterdir()):
            raise RuntimeError(f"{path} exists, is no worktree of this repository and is not empty")
        shutil.rmtree(path)


def _reusable(p: Project, path: Path, branch: str) -> tuple[bool, dict | None]:
    """(whether the worktree at `path` is whole, git's record of it); see _whole."""
    rec = _registered(p, path)
    if rec and not rec["prunable"] and path.is_dir():
        return _whole(p, path, rec, branch), rec
    return False, rec


def ensure(p: Project, task: dict) -> tuple[Path, str]:
    """Create (or reuse) the task's worktree. A retried task continues on its own branch. A task
    carrying a branch (`pr_branch:<branch>`) works on that branch when it may (on_carried), else on
    its own branch from that branch's head. A git command that times out (git_timeout) or fails
    because an earlier, timed-out attempt left the branch or worktree behind is recovered here: a
    whole worktree is reused, a positively half-made one removed and made again (_whole), up to
    `worktree.create_attempts` (default 2) attempts, each with a longer timeout. A check that fails
    raises and leaves the worktree as it is: an error never counts as half-made. A branch is never
    deleted: one with commits beyond where it would start is checked out as it is; one without is
    reset to that start."""
    carried = on_carried(p, task) if not task.get("branch") else ""
    own = f"ttp/t{task['id']}-{slug(task['title'])}"
    path = p.worktrees / f"t{task['id']}"
    if carried and path.exists() and (_registered(p, path) or {}).get("branch") == own:
        # An earlier attempt, when the carried branch was held elsewhere, made it on the task's own
        # branch and failed before the branch was stored: it stays there.
        carried = ""
    branch = task.get("branch") or carried or own
    attempts = max(1, int((p.config().get("worktree") or {}).get("create_attempts") or 2))
    for n in range(attempts):
        whole, rec = _reusable(p, path, branch)
        if whole:
            break
        try:
            if path.exists() or rec:
                _clear_partial(p, path, rec)
            _create(p, task, path, branch, carried, git_timeout(p, 1 + n / 2))
            break
        except (RuntimeError, subprocess.SubprocessError):
            if n + 1 == attempts:
                if _reusable(p, path, branch)[0]:
                    break
                raise
    prepare(p, path)
    return path, branch


def checkout_env(p: Project) -> dict | None:
    """The environment of the git commands that check a task's worktree out: Git LFS files stay
    pointers (GIT_LFS_SKIP_SMUDGE=1), so a checkout never downloads large objects and outlasts its
    timeout; a task that needs them runs `git lfs pull` in its worktree. `worktree.lfs_smudge: true`
    downloads them at checkout. None (git's own environment) then."""
    if (p.config().get("worktree") or {}).get("lfs_smudge"):
        return None
    return {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}


def _create(p: Project, task: dict, path: Path, branch: str, carried: str, timeout: float) -> None:
    p.worktrees.mkdir(parents=True, exist_ok=True)
    env = checkout_env(p)
    exists = _git(p.root, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists and carried:
        _git(p.root, "worktree", "add", str(path), branch, timeout=timeout, env=env)
        # A local copy left behind by the remote's catches up, fast-forward only.
        _git(path, "merge", "--ff-only", "--quiet", f"origin/{branch}", check=False, timeout=timeout, env=env)
        return
    if not exists:
        _git(p.root, "fetch", "--quiet", "origin", check=False, timeout=timeout)
    # A rebase starts from the branch it rebases, even when it also continues a review.
    start = carried_head(p, task) if carried or rebased_branch(task) else None
    start = start or (None if carried else continued_head(p, task)) or carried_head(p, task)
    try:
        start = start or resolve_base(p)
    except RuntimeError:
        if not exists:
            raise
    if exists and (not start or subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", branch,
                                                start], capture_output=True, timeout=timeout).returncode != 0):
        # Commits beyond its start (a retried task's own work): checked out as it is, never reset.
        _git(p.root, "worktree", "add", str(path), branch, timeout=timeout, env=env)
        return
    # New, or left behind with nothing of its own by an attempt that timed out: (re)made at its start.
    _git(p.root, "worktree", "add", "-B" if exists else "-b", branch, str(path), start, timeout=timeout, env=env)


_VALID_BRANCHES: dict[str, bool] = {}


def valid_branch(name: str) -> bool:
    """Whether `name` is one valid branch name (`git check-ref-format --branch`): never prose, several
    words, an option-like `-x`, main/master or HEAD (a task never carries a protected branch)."""
    if not name or name != name.strip() or any(c.isspace() for c in name) or name.startswith("-") \
            or name in ("HEAD", "main", "master"):
        return False
    if name not in _VALID_BRANCHES:
        r = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, text=True)
        _VALID_BRANCHES[name] = r.returncode == 0 and r.stdout.strip() == name
    return _VALID_BRANCHES[name]


# A task that carries no branch, even one the task it continues carries (push.pr_branch_of): a redo
# of a mislabelled task that lands on the push branch.
NO_PR_BRANCH = "no_pr_branch"


def carried_branch(task: dict) -> str:
    """The branch a task's `pr_branch:<branch>` label names (it delivers onto it), or "" (also when
    the label's value is not a valid branch name: valid_branch)."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    found = next((lb[10:] for lb in labels if isinstance(lb, str) and lb.startswith("pr_branch:")), "")
    return found if valid_branch(found) else ""


def rebased_branch(task: dict) -> str:
    """The branch a task's `rebases:<branch>` label names: it starts from that branch's head on its own
    branch and delivers there (a rebase cannot fast-forward the old one), or ""."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    found = next((lb[8:] for lb in labels if isinstance(lb, str) and lb.startswith("rebases:")), "")
    return found if valid_branch(found) else ""


def on_carried(p: Project, task: dict) -> str:
    """The task's carried branch when its worktree may check it out: it exists here or on origin,
    no other worktree holds it (the task's own, which an earlier attempt made on it, does not
    count), and it is no shared branch (main/master, the push branch or the base work starts from).
    "" otherwise: the task then works on its own branch."""
    branch = carried_branch(task)
    if not branch:
        return ""
    d = p.config().get("delivery") or {}
    shared = {"main", "master"}
    for ref in (str(d.get("push_branch") or "").strip(), named_base(p)):
        shared |= {ref, ref.partition("/")[2]} if ref else set()
    if branch in shared:
        return ""
    own, path = p.worktrees / f"t{task['id']}", ""
    for line in _git(p.root, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == f"branch refs/heads/{branch}" and path not in (str(own), str(own.resolve())):
            return ""
    _git(p.root, "fetch", "--quiet", "origin", check=False)
    return branch if carried_head(p, task) else ""


LINK_PATHS = [".venv"]   # worktree.link_paths: the default
NEVER_OWN_KINDS = ("review", "harness")   # reviews work in the change's worktree, harness tasks in the harness


def gets_worktree(p: Project, kind: str) -> bool:
    """Whether a task of this kind runs in a worktree of its own: code always; another kind when
    `worktree.kinds` lists it (off by default: a fresh worktree lacks the root's untracked build
    trees, outputs and submodule checkouts that such tasks often use). Never review or harness."""
    if kind == "code":
        return True
    kinds = (p.config().get("worktree") or {}).get("kinds")
    return kind not in NEVER_OWN_KINDS and isinstance(kinds, list) and kind in kinds


# The tasks that ran in a worktree of their own, as SQL over tasks: code, and another kind
# gets_worktree gave one (its branch is ttp/t<id>-...). Their commits are on that branch only.
OWN_WORKTREE_SQL = "(kind='code' OR (kind NOT IN ('review','harness') AND branch LIKE 'ttp/t' || id || '-%'))"


def own_worktree(task) -> bool:
    """OWN_WORKTREE_SQL for one task row."""
    t = dict(task)
    return t.get("kind") == "code" or (t.get("kind") not in NEVER_OWN_KINDS
                                       and str(t.get("branch") or "").startswith(f"ttp/t{t.get('id')}-"))


def _ignores(repo: Path, rel: str) -> bool:
    """Git ignores `rel` in `repo` (a tracked path never counts as ignored)."""
    return subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
                          capture_output=True, timeout=120).returncode == 0


def prepare(p: Project, path: str | Path, root: str | Path | None = None) -> list[str]:
    """Set up a worktree of the project's repository the same way whatever made it: a code or fix
    task's (ensure), one a reviewer works in or made itself, the push queue's and after_push's
    checkouts. Today that is link_paths from the main checkout `root` (the project root by default).
    Nothing happens for the main checkout itself or a worktree of another repository. Returns the
    entries linked; failures are skipped."""
    root = Path(root or p.root)
    try:
        path = Path(path)
        top = Path(_git(path, "rev-parse", "--show-toplevel"))
        if top.resolve() == root.resolve():
            return []
        common = lambda d: (Path(d) / _git(d, "rev-parse", "--git-common-dir")).resolve()   # noqa: E731
        if common(top) != common(root):
            return []
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []
    return link_paths(p, top, root)


def link_paths(p: Project, path: Path, root: str | Path | None = None) -> list[str]:
    """Symlink the main checkout's (`root`, the project root by default) git-ignored environment
    entries (`worktree.link_paths`, relative paths) into the worktree at `path`, so a check that runs
    `.venv/bin/python` works in a fresh worktree. An entry is linked only when it exists in the checkout, git ignores it there (so
    it is not tracked) and nothing is at its place in the worktree, tracked or not. A rule like
    `.venv/` matches directories only, not a link, so a link git would not ignore gets a `/<entry>`
    line in the repository's info/exclude: `git add -A` never commits it and the worktree still
    counts as clean. Removing the worktree deletes the link, never what it points to. Returns the
    entries linked; failures are skipped."""
    rels = (p.config().get("worktree") or {}).get("link_paths", LINK_PATHS)
    if not isinstance(rels, list):
        return []
    root, path, done = Path(root or p.root), Path(path), []
    for rel in rels:
        if not isinstance(rel, str) or not rel.strip("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
            continue
        rel = rel.strip("/")
        src, dest, made = root / rel, path / rel, False
        try:
            if (not src.exists() or os.path.lexists(dest) or not dest.parent.is_dir()
                    or not _ignores(root, rel) or _git(path, "ls-files", "--", rel, check=False)):
                continue
            os.symlink(src.resolve(), dest, target_is_directory=src.is_dir())
            made = True
            if not _ignores(path, rel):
                common = Path(path) / _git(path, "rev-parse", "--git-common-dir")
                exclude = common / "info" / "exclude"
                text = exclude.read_text() if exclude.is_file() else ""
                if f"/{rel}" not in text.splitlines():
                    durable_write(exclude, text + ("" if not text or text.endswith("\n") else "\n") + f"/{rel}\n")
            done.append(rel)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            if made:   # a link git might commit goes again
                dest.unlink(missing_ok=True)
    return done


VENV_NAMES = (".venv", "venv")


def _is_venv(path: Path) -> bool:
    """A usable Python virtual environment: pyvenv.cfg and an interpreter that exists (a venv
    whose base Python was removed has a dangling bin/python)."""
    return (path / "pyvenv.cfg").is_file() and any((path / b).exists() for b in ("bin/python", "Scripts/python.exe"))


def project_venv(p: Project, cwd: str | Path | None = None) -> Path | None:
    """The project's venv for a run in `cwd` to use (see `worktree.venv` in the config), or None:
    none is set up, it is turned off, or `cwd` has another venv of its own (VENV_NAMES)."""
    want = (p.config().get("worktree") or {}).get("venv", "auto")
    if not want or not isinstance(want, str):
        return None
    if want == "auto":
        cands = [p.root / n for n in VENV_NAMES]
    else:
        q = Path(want).expanduser()
        cands = [q if q.is_absolute() else p.root / q]
    found = next((c.resolve() for c in cands if _is_venv(c)), None)
    own = [v.resolve() for n in VENV_NAMES for v in [Path(cwd or ".") / n] if cwd and _is_venv(v)]
    return None if own and found not in own else found


def venv_env(venv: Path, path: str) -> dict[str, str]:
    """Environment that activates `venv` in front of `path` (PATH), as its activate script does."""
    bindir = venv / ("Scripts" if (venv / "Scripts/python.exe").exists() else "bin")
    return {"VIRTUAL_ENV": str(venv), "PATH": f"{bindir}{os.pathsep}{path}"}


def continued_head(p: Project, task: dict) -> str | None:
    """The head of the branch of the task this one continues (one that ran in a worktree of its
    own, own_worktree), so its commits carry over; for a continued review, the head of what it
    reviewed (reviewed_head), so a re-plan of a failed review builds on the reviewed branch.
    A branch of its own lets the old worktree stay checked out until it is pruned."""
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if old and old["kind"] == "review":
        return reviewed_head(p, old)
    if not old or not own_worktree(old) or not old["branch"]:
        return None
    for cand in (old["branch"], f"origin/{old['branch']}"):
        head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
        if head:
            return head
    return None


def reviewed_head(p: Project, review: dict) -> str | None:
    """The commit a review reviewed: of the refs it covers (reviewed_refs) that resolve here or on
    origin, the one that contains all the others (a fix branch on top of the reviewed branch), else
    the first one named. None when none resolves."""
    heads = []
    for ref in reviewed_refs(p, review):
        for cand in (ref,) if _HEX.fullmatch(ref) else (ref, f"origin/{ref}"):
            head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
            if head:
                heads.append(head)
                break
    heads = list(dict.fromkeys(heads))
    for tip in heads:
        if all(h == tip or subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", h, tip],
                                          capture_output=True, timeout=120).returncode == 0 for h in heads):
            return tip
    return heads[0] if heads else None


def carried_head(p: Project, task: dict) -> str | None:
    """The head of the task's carried branch (carried_branch), so it builds on what it delivers onto,
    or of the branch it rebases (rebased_branch)."""
    branch = carried_branch(task) or rebased_branch(task)
    for cand in (branch, f"origin/{branch}") if branch else ():
        head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
        if head:
            return head
    return None


def resolve_base(p: Project) -> str:
    """The configured base as a commit git can find: the name itself, else the remote's branch of
    that name (a fresh clone has `origin/<name>` but no local `<name>`). A miss names close matches,
    so whoever set it can fix it in one step."""
    ref = base_ref(p)
    for cand in (ref, f"origin/{ref}"):
        if _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False):
            return cand
    tail = ref.split("/")[-1]
    near = [b for b in _git(p.root, "branch", "-a", "--format=%(refname:short)", check=False).split()
            if tail and tail in b][:5]
    raise RuntimeError(f"base branch {ref!r} not found here or on origin"
                       + (f"; similar: {', '.join(near)}" if near else ""))


def remove(p: Project, task_id: int, force: bool = False) -> None:
    """Remove a task's worktree; its branch stays. Call only once keep_reason() found nothing to lose.
    Ignored files go with it (sweep archives hand-off artifacts first). No --force: git itself then
    refuses a worktree with changes or with submodules, whose commits may live only in the worktree's
    own git directory. `force` only once its tracked changes are in a verified archived patch."""
    path = p.worktrees / f"t{task_id}"
    if path.exists():
        _git(p.root, "worktree", "remove", *(["--force"] if force else []), str(path))
    _git(p.root, "worktree", "prune", check=False)


def keep_reason(path: Path) -> str | None:
    """Why this worktree must stay, or None when removing it loses nothing: no submodules set up in
    it, no uncommitted or untracked files, and its HEAD is on a local or remote branch (the task's
    own branch is never deleted, so its commits stay reachable)."""
    return inspect(path)["why"]


def inspect(path: Path) -> dict:
    """keep_reason with what lies behind it: `why` (None: nothing to lose), `dirty` "untracked" (the
    only dirty entries are untracked files, `untracked` lists them, HEAD is on a branch, no submodules)
    or "tracked" (modified tracked files, `tracked` lists their paths, `fingerprint` hashes the
    status and diff), else None."""
    import hashlib

    def git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=text, timeout=300)
    out = {"why": None, "dirty": None}
    # A submodule's repository lives in the worktree's own git directory (modules/), so commits made
    # in it exist nowhere else and removing the worktree would delete them. Such a worktree stays.
    gd = git("rev-parse", "--absolute-git-dir")
    if gd.returncode != 0:
        return {**out, "why": "git rev-parse failed"}
    modules = Path(gd.stdout.strip()) / "modules"
    subs = git("submodule", "status")
    if subs.returncode != 0:
        return {**out, "why": "git submodule status failed"}
    inited = [ln.split()[1] for ln in subs.stdout.splitlines() if ln.strip() and not ln.startswith("-")]
    if inited or (modules.is_dir() and any(modules.iterdir())):
        return {**out, "why": "has submodules set up (" + (", ".join(inited[:3]) or "modules/") + "); their commits "
                "may exist only here, so it is never removed automatically"}
    st = git("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")
    if st.returncode != 0:
        return {**out, "why": "git status failed"}
    dirty = st.stdout.splitlines()
    held = git("for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)", "refs/heads", "refs/remotes")
    if held.returncode != 0:
        return {**out, "why": "git for-each-ref failed"}
    detached = None if held.stdout.strip() else "commits on no branch (detached HEAD)"
    if not dirty:
        return {**out, "why": detached}
    why = f"uncommitted changes in {len(dirty)} path(s), e.g. {dirty[0][3:].strip()[:80]}"
    tracked = [ln[3:].strip() for ln in dirty if not ln.startswith("??")]
    if tracked:
        diff = git("diff", "HEAD", "--binary", text=False)
        fp = hashlib.sha256(st.stdout.encode() + b"\0" + diff.stdout).hexdigest()[:16]
        return {**out, "why": why, "dirty": "tracked", "tracked": tracked, "fingerprint": fp}
    if detached:
        return {**out, "why": f"{why}; {detached}"}
    files = git("ls-files", "--others", "--exclude-standard", "-z")
    if files.returncode != 0:
        return {**out, "why": why}
    return {**out, "why": why, "dirty": "untracked", "untracked": [e for e in files.stdout.split("\0") if e]}


def move_leftovers(path: Path, rels: list[str], dest: Path) -> int:
    """Move these untracked files (relative to the worktree) under `dest`, keeping their relative
    paths; a name already there gets a numbered suffix. Returns how many moved."""
    import shutil
    moved = 0
    for rel in rels:
        src, to = path / rel, dest / rel
        n = 1
        while to.exists() or to.is_symlink():
            to = dest / f"{rel}.{n}"
            n += 1
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(to))
        moved += 1
    return moved


def _size(path: Path, rels: list[str]) -> int:
    """Bytes these entries take (a directory: everything in it), links not followed. A directory
    holding a network or FUSE mount is skipped: a hung one would block the walk in D state."""
    from .daemon import network_mounts
    nets = network_mounts()
    total = 0
    for rel in rels:
        full = path / rel
        if any(m == full or full in m.parents for m in nets):
            continue
        try:
            if full.is_dir() and not full.is_symlink():
                total += sum(f.lstat().st_size for f in full.rglob("*") if f.is_symlink() or f.is_file())
            else:
                total += full.lstat().st_size
        except OSError:
            continue
    return total


def last_run_dir(p: Project, task_id: int) -> Path | None:
    r = p.db.one("SELECT id, dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (task_id,))
    return (Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) if r else None


LEFTOVERS_DIR = "worktree-leftovers"
DIRTY_EVENT = "worktree_uncommitted"   # the coordinator's event for a finished worktree's tracked changes
LEFTOVERS_MAX_MB = 50   # disk.worktree_leftovers_max_mb: untracked files up to this total move out (see sweep)


# Build output and tool caches: regenerated on demand, often gigabytes. Only ignored entries are
# cleared, so a tracked file or an uncommitted new one (which keeps the worktree) is never touched.
CACHE_DIRS = ["build", "_build", "cmake-build-*", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules", "*.egg-info", ".eggs",
              ".gradle", ".ccache"]


def _ignored(path: Path) -> set[str] | None:
    """Git-ignored entries in a worktree, relative to it. With --directory git lists a directory
    itself only when everything in it is ignored. None when git fails."""
    out = subprocess.run(["git", "-C", str(path), "ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "-z"], capture_output=True, text=True, timeout=300)
    return {e.rstrip("/") for e in out.stdout.split("\0") if e} if out.returncode == 0 else None


def handoff_paths(p: Project) -> list[tuple[int, str]]:
    """(task, entry) for each `artifacts` entry of every hand-off (result.json) any run wrote. Other
    tasks often leave files in an earlier task's worktree, so a sweep reads them all, once."""
    import json
    out = []
    for r in p.db.q("SELECT id, task, dir FROM runs WHERE task IS NOT NULL"):
        try:
            data = json.loads(((Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) / "result.json").read_text())
        except (OSError, ValueError):
            continue
        arts = data.get("artifacts") if isinstance(data, dict) else None
        out += [(r["task"], a) for a in arts if isinstance(a, str)] if isinstance(arts, list) else []
    return out


def _existing(entry: str, bases: list[Path]) -> list[Path]:
    """The files an artifact entry names. Entries may be absolute or relative to one of `bases`,
    may be globs (tmp/px_*.pt), and may carry trailing text ("out.mp4 (the video)", "run.log:12"),
    so the longest leading part that exists wins."""
    import glob
    words = entry.strip().split()
    for n in range(len(words), 0, -1):
        text = " ".join(words[:n])
        for cand in dict.fromkeys((text, text.rstrip(".,;:)]}'\"`"), re.sub(r"(:\d+)+[.,;:]?$", "", text))):
            cand = cand.strip("'\"`(")
            if not cand or "://" in cand:
                continue
            q = Path(cand).expanduser()
            for full in ([q] if q.is_absolute() else [b / q for b in bases]):
                try:
                    if full.exists():
                        return [full]
                    found = [Path(m) for m in glob.glob(str(full), recursive=True)] if any(
                        c in cand for c in "*?[") else []
                    if found:
                        return found
                except OSError:
                    continue
    return []


def handoff_artifacts(p: Project, task_id: int, path: Path, ignored: set[str] | None = None,
                      entries: list[tuple[int, str]] | None = None) -> list[str]:
    """Hand-off artifacts (any task's result.json `artifacts`, see handoff_paths) that still exist
    inside this task's worktree and that git ignores, relative to the worktree. An entry is read
    relative to its own task's worktree or the project. `git worktree remove` deletes ignored files,
    and only those: tracked ones are on the branch, other untracked ones keep the worktree."""
    ignored = _ignored(path) if ignored is None else ignored
    if not ignored:
        return []
    root = path.resolve()
    found: list[str] = []
    for tid, entry in handoff_paths(p) if entries is None else entries:
        for full in _existing(entry, [path if tid == task_id else p.worktrees / f"t{tid}", p.root, p.base]):
            try:
                rel = full.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if rel != "." and rel not in found and any(
                    rel == e or rel.startswith(e + "/") or e.startswith(rel + "/") for e in ignored):
                found.append(rel)
    return found


def clear_caches(path: Path, names: list[str] | None = None, keep: list[str] = ()) -> list[str]:
    """Delete ignored build and cache directories in a worktree. A cache-named directory goes only
    when git ignores it wholly; else just the ignored entries named like one inside it. Nothing that
    is, holds or lies in a path of `keep` (hand-off artifacts) goes. Returns what went."""
    import fnmatch
    import shutil
    pats = CACHE_DIRS if names is None else names
    listed = _ignored(path)
    if listed is None:
        return []
    found: set[str] = set()
    for entry in listed:
        parts = entry.split("/")
        for i, part in enumerate(parts):
            prefix = "/".join(parts[:i + 1])
            if prefix in listed and any(fnmatch.fnmatchcase(part, pat) for pat in pats):
                found.add(prefix)
                break
    gone = []
    for rel in sorted(found):
        if any(rel.startswith(g + "/") for g in gone) or any(
                k == rel or k.startswith(rel + "/") or rel.startswith(k + "/") for k in keep):
            continue
        target = path / rel
        try:
            if target.is_symlink() or not target.is_dir():
                target.unlink()
            else:
                shutil.rmtree(target)
            gone.append(rel)
        except FileNotFoundError:
            continue
        except OSError:
            pass
    return gone


def needed_by(task: dict, open_tasks: list[dict]) -> dict | None:
    """The first unfinished task that may still work in this task's worktree (a review that runs
    `ttp push` there, say): it depends on or continues the task, or its spec names the task's
    branch or id (#12, t12, task 12, worktrees/t12)."""
    from .db import dependency_ids
    tid, branch = task["id"], task.get("branch")
    named = re.compile(rf"(?<![\w.-])(?:#|t|task\s+){tid}(?!\d)"
                       + (rf"|(?<![\w/.-]){re.escape(branch)}(?![\w/-])" if branch else ""), re.I)
    for t in open_tasks:
        if t["id"] != tid and (tid in dependency_ids(t) or continues_id(t) == tid or named.search(t.get("spec") or "")):
            return t
    return None


# A finished task's worktree stays at least this long, and while the coordinator has not yet seen how
# the task ended: the review that pushes from it is often queued only after that.
FINISH_GRACE_S = 3600


def held_by(p: Project, task: dict, open_tasks: list[dict], now: float) -> str | None:
    """Why a finished task's worktree must stay untouched for now, or None."""
    from .push import running
    path = (p.worktrees / f"t{task['id']}").resolve()
    if any(Path(m.get("repo") or "/").resolve() == path for m in running(p)):
        return "a detached `ttp push` runs in it"
    user = needed_by(task, open_tasks)
    if user:
        return f"task #{user['id']} ({user['status']}) may still use it"
    if p.db.one("SELECT id FROM events WHERE task=? AND status='queued' AND kind!=? LIMIT 1",
                (task["id"], DIRTY_EVENT)):
        return "the coordinator has not yet seen how it ended"
    if now - float(task["updated"] or now) < FINISH_GRACE_S and not review_over(p, task):
        return f"it ended under {FINISH_GRACE_S // 60} min ago; kept for a review"
    return None


def review_over(p: Project, task: dict) -> bool:
    """Whether the review a finished task's worktree is kept for is over: the task is itself a
    review, or a review of it has ended (passed or failed) and the coordinator has seen that. A failed
    review's fix continues the task, so needed_by keeps the worktree for it."""
    from .db import TERMINAL_TASK_STATES, dependency_ids
    if task.get("kind") == "review":
        return True
    rows = p.db.q("SELECT id, depends_on FROM tasks WHERE kind='review' AND status IN (%s) AND depends_on LIKE ?"
                  % ",".join("?" * len(TERMINAL_TASK_STATES)), (*TERMINAL_TASK_STATES, f"%{task['id']}%"))
    return any(task["id"] in dependency_ids(r) and not p.db.one(
        "SELECT id FROM events WHERE task=? AND status='queued' LIMIT 1", (r["id"],)) for r in rows)


HARNESS_LOCKS = ("initializing",)   # worktree lock reasons git or the harness sets; cleared once stale
ARCHIVE_MAX_MB = 200   # disk.worktree_archive_max_mb: unnamed untracked files archived per task, the rest dropped


def _small_logs(path: Path, limit: int = 200) -> list[str]:
    """Ignored *.log files of at most archive.LOG_MAX_MB each (up to `limit`), relative to the worktree:
    `git worktree remove` would delete them, and they are cheap to keep."""
    from . import archive
    out = []
    for f in archive.files_of(path, sorted(_ignored(path) or [])):
        try:
            if f.endswith(".log") and not (path / f).is_symlink() and (path / f).stat().st_size <= archive.LOG_MAX_MB * 1e6:
                out.append(f)
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _users(path: Path) -> list[int]:
    """Live processes whose working directory is in `path` (from /proc; none known without it)."""
    root = path.resolve()
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []
    out = []
    for pid in pids:
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if cwd == root or root in cwd.parents:
            out.append(int(pid))
    return out


def sweep(p: Project, *, older_than_s: float = 0, names: list[str] | None = None,
          skip=lambda task: False, leftovers_max_mb: float | None = None, archive_max_mb: float | None = None,
          patch_tracked: bool = False, archive_limit: int | None = None, deadline: float | None = None,
          reserve_bytes: float = 0, progress=lambda: None, dry_run: bool = False) -> list[dict]:
    """Tidy the worktrees of finished tasks (done, failed, cancelled) with no run or process still
    going: clear their build and cache directories, then remove each one whose removal loses nothing
    (see keep_reason). A worktree that may still be wanted (see held_by: an unfinished task needs it,
    the coordinator has not seen the finish yet, or it ended under FINISH_GRACE_S ago and its review
    is not over) is left as it is, and reported with `held` set.
    Files outside git go first: hand-off artifacts (any task's result.json `artifacts`, see
    handoff_artifacts; ignored or untracked) are archived (archive.store, state/archive/tasks/<id>/,
    reported in `archived`). Other untracked files (HEAD on a branch) move to the last run's
    directory when they total at most `leftovers_max_mb` (see leftovers; reported in `moved`); larger
    ones are listed in the archive's manifest and kept there up to `archive_max_mb` (None:
    ARCHIVE_MAX_MB), smallest first, the rest dropped. Small ignored logs (*.log up to
    archive.LOG_MAX_MB each) are archived with them. With `patch_tracked` (the disk guard's sweep,
    `ttp prune --patch-tracked`), a done or cancelled task's tracked changes go into a binary patch
    verified to apply to its head (archive.tracked_patch) and the worktree is removed, when that head
    is on a branch. `leftovers_max_mb` 0 or less keeps the worktree
    of untracked files as it is. One with modified tracked files is kept and reported with `tracked`
    (paths) and `fingerprint`. A stale lock git or the harness set (HARNESS_LOCKS: a `git worktree
    add` that never finished) is cleared (`unlocked`); a half-made worktree goes. Any other lock keeps
    the worktree. Branches stay, so a task that `continues` one starts from its commits. `dry_run`
    changes nothing and reports what a sweep would do (`why` None: it would go). One sweep at a time
    per project; a busy lock returns no results.
    Removing, unlocking and clearing caches is cheap and always done. Archiving (hashing, compressing,
    patching) is the costly part: `archive_limit` caps how many worktrees one call archives, and none
    starts after `deadline` (a time.time()); the others are kept with `deferred` set, for a later
    call (the daemon archives one per tick step, `ttp prune` all). `progress()` is called per worktree
    and, while archiving, per file. An archive that does not fit with `reserve_bytes` free on
    state/'s filesystem is skipped and the worktree kept (archive.NoRoom; `why` names the sizes); an
    archiving cut off part way is finished or rolled back first (archive.recover)."""
    import fcntl
    import time
    from . import archive
    from .db import TERMINAL_TASK_STATES
    if not p.worktrees.is_dir():
        return []
    p.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.state / "prune.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []
        out = []
        now = time.time()
        entries = None
        records: dict | None = None
        limit = LEFTOVERS_MAX_MB if leftovers_max_mb is None else float(leftovers_max_mb)
        cap = ARCHIVE_MAX_MB if archive_max_mb is None else max(float(archive_max_mb), 0.0)
        archived = 0

        def costly_ok() -> bool:
            return dry_run or ((archive_limit is None or archived < archive_limit)
                               and (deadline is None or time.time() < deadline))
        deferred = "archiving deferred to a later sweep (each daemon tick step archives at most one worktree)"
        open_tasks = p.db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE status NOT IN (%s)"
                            % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)
        for path in sorted(p.worktrees.iterdir()):
            m = re.fullmatch(r"t(\d+)", path.name)
            task = p.db.task(int(m.group(1))) if m else None
            if (not task or task["status"] not in TERMINAL_TASK_STATES
                    or now - float(task["updated"] or now) < older_than_s or skip(task)
                    or p.db.one("SELECT id FROM runs WHERE task=? AND status='running' LIMIT 1", (task["id"],))):
                continue
            if records is None:
                try:
                    records = _records(p)
                except Exception:
                    records = {}
            rec = _record_of(records, path)
            lock = rec["locked"] if rec else None
            half = lock is not None and lock.startswith(HARNESS_LOCKS) and not (path / ".git").is_file()
            if not half and not is_git(path):
                continue
            res = {"task": task["id"], "path": str(path), "branch": task["branch"], "status": task["status"],
                   "updated": task["updated"], "cleared": []}
            why = held_by(p, task, open_tasks, now)
            users = [] if why else _users(path)
            if users:
                why = f"process {users[0]} runs in it" + (f" (+{len(users) - 1} more)" if len(users) > 1 else "")
            if why:
                out.append({**res, "held": True, "why": why})
                continue
            progress()
            try:
                if not dry_run and archive.task_dir(p, task["id"]).is_dir():
                    archive.recover(p, task["id"])
                if lock is not None:
                    if not lock.startswith(HARNESS_LOCKS):
                        out.append({**res, "why": f"locked ({lock or 'no reason given'}): only a stale lock git "
                                                  f"or the harness set is cleared"})
                        continue
                    res["unlocked"] = lock
                    if not dry_run:
                        if half:
                            _clear_partial(p, path, rec)
                        else:
                            _git(p.root, "worktree", "unlock", str(path), timeout=git_timeout(p))
                    if half:
                        out.append({**res, "why": None, "half": True})
                        continue
                entries = handoff_paths(p) if entries is None else entries
                arts = handoff_artifacts(p, task["id"], path, entries=entries)
                res["cleared"] = [] if dry_run else clear_caches(path, names, keep=arts)
                info = inspect(path)
                why = info["why"]
                others: list[str] = []
                patch = None
                if info["dirty"] == "tracked" and patch_tracked and task["status"] in ("done", "cancelled") \
                        and _git(path, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/heads", "refs/remotes",
                                 check=False).strip():
                    if not costly_ok():
                        out.append({**res, "why": deferred, "deferred": True})
                        continue
                    patch = archive.tracked_patch(path)
                    res["patched"] = info["tracked"]
                    untracked = _git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
                    untracked = [u for u in untracked if u]
                    arts += handoff_artifacts(p, task["id"], path, ignored=set(untracked), entries=entries)
                    others = [u for u in untracked if not any(u == a or u.startswith(a + "/") for a in arts)]
                    why = None
                elif info["dirty"] == "tracked":
                    res.update(tracked=info["tracked"], fingerprint=info["fingerprint"])
                elif info["dirty"] == "untracked" and limit <= 0:
                    why = (f"uncommitted untracked files only ({len(info['untracked'])}); moving them out is off "
                           f"(disk.worktree_leftovers_max_mb)")
                elif info["dirty"] == "untracked":
                    untracked = info["untracked"]
                    arts += handoff_artifacts(p, task["id"], path, ignored=set(untracked), entries=entries)
                    others = [u for u in untracked if not any(u == a or u.startswith(a + "/") for a in arts)]
                    why = None
                    size, run = (_size(path, others), last_run_dir(p, task["id"])) if others else (0, None)
                    if others and size <= limit * 1e6 and run is not None:
                        res["moved"] = {"files": len(others), "bytes": size, "to": str(run / LEFTOVERS_DIR)}
                        if not dry_run:
                            move_leftovers(path, others, run / LEFTOVERS_DIR)
                        others = []
                if why is None:
                    others += [f for f in _small_logs(path) if f not in others and not any(
                        f == a or f.startswith(a + "/") for a in arts)]
                if why is None and (arts or others or patch) and not costly_ok():
                    out.append({**res, "why": deferred, "deferred": True})
                    continue
                if why is None and (arts or others or patch):
                    if not dry_run:
                        archived += 1
                    try:
                        res["archived"] = (archive.plan(path, arts, others, cap, patch) if dry_run else archive.store(
                            p, task, path, arts, others, cap, head=_git(path, "rev-parse", "HEAD", check=False),
                            patch=patch, tracked=res.get("patched") or [], reserve_bytes=reserve_bytes,
                            progress=progress))
                    except archive.NoRoom as e:
                        archived -= 1   # nothing was written: it does not use up the step's archiving
                        if res.pop("patched", None):
                            res.update(tracked=info["tracked"], fingerprint=info["fingerprint"])
                        out.append({**res, "why": str(e)})
                        continue
                if why is None and not dry_run and not patch and (res.get("moved") or res.get("archived")):
                    why = keep_reason(path)
                res["why"] = why
                if why is None and not dry_run:
                    remove(p, task["id"], force=bool(patch))
            except Exception as e:
                res["why"] = f"error: {e}"[:300]
            out.append(res)
        return out
    finally:
        os.close(fd)


def has_changes(path: Path, since_ref: str) -> bool:
    """Durable progress for a code task: commits past the base, or uncommitted edits."""
    ahead = _git(path, "rev-list", "--count", f"{since_ref}..HEAD", check=False)
    dirty = _git(path, "status", "--porcelain", check=False)
    return (ahead.isdigit() and int(ahead) > 0) or bool(dirty)


_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def reviewed_refs(p: Project, task: dict) -> list[str]:
    """What a review task reviews: named_refs, and the fix branches that build on them (fix_branches)."""
    refs = named_refs(p, task)
    return list(dict.fromkeys(refs + list(fix_branches(p, task, refs))))


def named_refs(p: Project, task: dict) -> list[str]:
    """The branches of the tasks a review depends on, and the task branches and commit hashes its
    spec names."""
    from .db import dependency_ids, names_branch
    spec = task.get("spec") or ""
    ids = [i for i in dependency_ids(task) if i is not None]
    refs = [r["branch"] for r in p.db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL AND branch!=''")
            if r["id"] in ids or names_branch(spec, r["branch"])]
    refs += _HEX.findall(spec)
    return list(dict.fromkeys(refs))


def fix_branches(p: Project, task: dict, refs: list[str]) -> dict[str, str]:
    """{fix branch: the reviewed branch it builds on}: for each branch in `refs`, the own branch of
    the newest code task that carries it, other than the review itself. A fix or re-plan of a failed
    review often cannot check the reviewed branch out (the reviewed task's worktree still holds it),
    so it commits on its own ttp/t<id>-... branch from that branch's head (ensure); its re-review,
    queued with it, names only the reviewed branch. A task carries a branch it labels
    `pr_branch:<branch>`, or the branch of the task it continues (a code task's branch, or a branch
    a continued review reviews). Failed and cancelled tasks carry nothing."""
    from .db import continues_id
    names = {r for r in refs if not _HEX.fullmatch(r)}
    if not names:
        return {}
    newest: dict[str, tuple[int, str]] = {}
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!='' AND id!=? "
                    "AND status NOT IN ('failed','cancelled') AND (labels LIKE '%\"pr_branch:%' "
                    "OR labels LIKE '%\"continues:%')", (task["id"],)):
        if t["branch"] in names:
            continue
        on = {carried_branch(t)} & names
        prev = p.db.task(c) if not on and (c := continues_id(t)) is not None else None
        if prev and prev["kind"] == "review":
            on = set(named_refs(p, prev)) & names
        elif prev:
            on = {prev.get("branch")} & names
        for ref in on:
            if ref not in newest or t["id"] > newest[ref][0]:
                newest[ref] = (t["id"], t["branch"])
    return {b: ref for ref, (_, b) in sorted(newest.items(), key=lambda kv: kv[1][0])}


def diff_lines(p: Project, refs: list[str]) -> dict[str, int | None] | None:
    """Lines changed per file by `refs` since they left the base branch (None for a binary file),
    or None when they change nothing measurable, such as a commit already on the base."""
    base = resolve_base(p)
    out: dict[str, int | None] = {}
    for ref in refs:
        if not _git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False):
            continue
        for line in _git(p.root, "diff", "--numstat", "--no-renames", f"{base}...{ref}").splitlines():
            added, deleted, path = line.split("\t", 2)
            prev = out.get(path, 0)
            n = None if added == "-" else int(added) + int(deleted)
            out[path] = None if n is None or prev is None else max(n, prev)
    return out or None


def _closest_ancestor(p: Project, heads: list[str], ref: str) -> str | None:
    """The head in `heads` that is an ancestor of `ref` with the fewest commits between them."""
    best: tuple[int, str] | None = None
    for head in heads:
        if not _git(p.root, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}", check=False):
            continue
        if subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", head, ref],
                          capture_output=True, timeout=120).returncode != 0:
            continue
        n = int(_git(p.root, "rev-list", "--count", f"{head}..{ref}") or 0)
        if best is None or n < best[0]:
            best = (n, head)
    return best[1] if best else None


FETCH_TIMEOUT_S = 30   # the local-only check's fetch; an unreachable remote must not hold up the tick


def dirty_tracked(repo: Path, timeout_s: float = 30) -> list[str] | None:
    """The tracked paths with uncommitted changes (staged or not) in the checkout at `repo`, sorted;
    untracked files and changes inside submodules are left out. None when `repo` is not a git work
    tree or git fails or is slow. Reads only: nothing is staged, committed or changed."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=no",
                              "--ignore-submodules=dirty"], capture_output=True, text=True, timeout=timeout_s,
                             stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    paths, items = set(), iter(out.stdout.split("\0"))
    for e in items:
        if len(e) > 3:
            paths.add(e[3:])
            if e[0] in "RC":
                next(items, None)   # a rename's or copy's source follows its new path
    return sorted(paths)


CHECKOUT_DIRTY_MAX = 2000   # checkout_state: past this many dirty paths, `dirty` is None (not compared)


def checkout_state(repo: Path, timeout_s: float = 30) -> dict | None:
    """Where the checkout at `repo` stands: `branch` (its short name, "" when HEAD is detached),
    `head` (the commit) and `dirty` (dirty_tracked; None past CHECKOUT_DIRTY_MAX or when git
    failed). None when `repo` is not a git work tree or git fails. Reads only."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"],
                              capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
        if head.returncode != 0:
            return None
        branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    dirty = dirty_tracked(repo, timeout_s)
    return {"branch": branch.stdout.strip() if branch.returncode == 0 else "", "head": head.stdout.strip(),
            "dirty": dirty if dirty is not None and len(dirty) <= CHECKOUT_DIRTY_MAX else None}


def local_only(repo: Path, branches: list[str], targets: list[str] = (), known: set = frozenset(),
               timeout_s: float = FETCH_TIMEOUT_S, pushed: set | frozenset = frozenset(),
               superseded: dict | None = None) -> tuple[dict, bool, dict] | None:
    """Which of `branches` hold finished work whose only copy is on this machine. Returns ({branch:
    (head, commits on no remote)}, whether the fetch of every remote worked, {branch: head} of the
    others), after that fetch; with a failed fetch the remote refs may be old, so a branch may be
    listed that the remote has. A branch is on a remote when a remote-tracking branch contains its
    head, or when its changes already are in one of `targets` (the branch it is delivered to, as
    `origin/<name>` or `<name>`, and each remote's default branch) although its head is not: a
    reviewer rebased, amended, squashed or batched it (see delivered). A (branch, head) in `known`
    was found on a remote before and is not looked at again. So is a branch whose head the push
    queue pushed or landed (`pushed`, see pushq.pushed_heads), or an ancestor of one, and a branch
    a task recorded as superseded (`superseded`, {branch: [(head, sha)]}, see is_superseded). A
    branch that no longer exists is left out. None when `repo` is not a git repository or has no remote: there
    is nothing to compare with. Pushes nothing."""
    try:
        if not _git(repo, "remote", check=False).split():
            return None
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--all", "--quiet"], capture_output=True,
                                 text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
    except subprocess.TimeoutExpired:
        fetched = False
    except (OSError, subprocess.SubprocessError):
        return None
    refs: list[str] = []
    for t in [*targets, *_git(repo, "for-each-ref", "--format=%(symref)", "refs/remotes/*/HEAD", check=False).split()]:
        for cand in (t, f"refs/remotes/{t}", f"refs/remotes/origin/{t}"):
            full = _git(repo, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", cand, check=False)
            if full.startswith("refs/remotes/"):
                if full not in refs:
                    refs.append(full)
                break
    out, clean = {}, {}
    for b in dict.fromkeys(branches):
        head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}^{{commit}}", check=False)
        if not head:
            continue
        if ((b, head) in known or queue_pushed(repo, head, pushed)
                or is_superseded(repo, head, (superseded or {}).get(b, ()))
                or _git(repo, "branch", "-r", "--contains", head, check=False)
                or any(delivered(repo, head, ref) for ref in refs)):
            clean[b] = head
            continue
        ahead = _git(repo, "rev-list", "--count", head, "--not", "--remotes", check=False)
        out[b] = (head, int(ahead) if ahead.isdigit() else 0)
    return out, fetched, clean


def is_superseded(repo: Path, head: str, records) -> bool:
    """Whether a task recorded `head`'s work as superseded by a commit a remote has: a record
    (recorded head, sha) whose head is `head` or contains it, and whose sha a remote-tracking branch
    contains. A branch that moved past its recorded head has work the record does not cover."""
    for rec_head, sha in records:
        if rec_head != head and subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, rec_head],
                                               capture_output=True, timeout=120).returncode != 0:
            continue
        if _git(repo, "branch", "-r", "--contains", sha, check=False):
            return True
    return False


def queue_pushed(repo: Path, head: str, pushed: set | frozenset) -> bool:
    """Whether the push queue pushed or landed `head` or a commit that contains it: `pushed` holds
    the heads of its pushed and landed rows (pushq.pushed_heads). Heads git no longer has count
    only by name."""
    if not pushed:
        return False
    if head in pushed:
        return True
    r = subprocess.run(["git", "-C", str(repo), "rev-list", "--ignore-missing", "-n1", head, "--not", *sorted(pushed)],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and not r.stdout.strip()


_DELIVERED: dict[tuple[str, str, str], bool] = {}   # (repo, head, ref commit) -> delivered(): both fixed, so is the answer
_DELIVERED_MAX = 4096


def delivered(repo: Path, head: str, ref: str, pushed: set | frozenset = frozenset()) -> bool:
    """Whether the changes of `head` since it left `ref` are already in `ref`: the push queue pushed
    or landed it (`pushed`, see queue_pushed), every commit has a patch-equivalent one there (`git
    cherry`: rebased or cherry-picked) or one with the same author, author date and subject (a rebase
    with conflicts, or an amend, keeps them; a subject alone is not enough), its whole change has the
    patch-id of one commit there (squashed into one), every file it changed is the same there
    (amended or squashed, maybe with other work), or its whole diff reverts cleanly from `ref`'s tree (batched, and later work
    touched the same files elsewhere). Remembered per (repo, head, commit of `ref`). The revert is only
    tried when `ref` changed every file `head` did since they parted (else it cannot apply): against
    a far-behind branch of a large repository the binary diff can take minutes. A git call that times
    out counts as not delivered."""
    if queue_pushed(repo, head, pushed):
        return True
    ref_commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if not ref_commit:
        return False
    key = (str(repo), head, ref_commit)
    if key not in _DELIVERED:
        if len(_DELIVERED) >= _DELIVERED_MAX:
            _DELIVERED.clear()
        try:
            _DELIVERED[key] = _delivered(repo, head, ref_commit)
        except subprocess.TimeoutExpired:
            _DELIVERED[key] = False   # too big to tell: not delivered, and the other branches still get checked
    return _DELIVERED[key]


def _patch_ids(repo: Path, cmd: list[str]) -> set[str]:
    """The `git patch-id --stable` ids of the patches git `cmd` prints."""
    out = subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True, timeout=120).stdout
    ids = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=out, capture_output=True,
                         timeout=120).stdout.decode(errors="replace")
    return {ln.split()[0] for ln in ids.splitlines() if ln.split()}


def _delivered(repo: Path, head: str, ref: str) -> bool:
    import tempfile
    base = _git(repo, "merge-base", ref, head, check=False)
    if not base:
        return False
    cherry = subprocess.run(["git", "-C", str(repo), "cherry", ref, head], capture_output=True, text=True, timeout=120)
    if cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines()):
        return True
    fmt = "--format=%an%x00%ae%x00%at%x00%s"
    mine = _git(repo, "log", "--no-merges", fmt, f"{base}..{head}", check=False).splitlines()
    if mine and set(mine) <= set(_git(repo, "log", "--no-merges", fmt, f"{base}..{ref}", check=False).splitlines()):
        return True
    files = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, check=False).split("\0")
    files = [f for f in files if f]
    # A squash of exactly this change touches only these files: scan only them, not the whole range.
    if len(mine) > 1 and files and (whole := _patch_ids(repo, ["diff", "--no-renames", base, head])) and (
            whole <= _patch_ids(repo, ["log", "-p", "--no-renames", "--no-merges", "--format=commit %H",
                                       f"{base}..{ref}", "--", *files])):
        return True
    if not files or not _git(repo, "diff", "--no-renames", "--name-only", ref, head, "--", *files, check=False):
        return True
    theirs = set(_git(repo, "diff", "--no-renames", "--name-only", "-z", base, ref, check=False).split("\0"))
    if not set(files) <= theirs:
        return False   # a file `ref` left as it was at `base` cannot hold `head`'s change to it
    patch = subprocess.run(["git", "-C", str(repo), "diff", "--binary", "--no-renames", base, head],
                           capture_output=True, timeout=120).stdout
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if subprocess.run(["git", "-C", str(repo), "read-tree", ref], capture_output=True, env=env,
                          timeout=120).returncode != 0:
            return False
        return subprocess.run(["git", "-C", str(repo), "apply", "--cached", "--reverse", "--check"], input=patch,
                              capture_output=True, env=env, timeout=120).returncode == 0
