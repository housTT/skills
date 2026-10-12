# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp landed`: whether a change is on the push branch, even after a rebase gave its commits new
shas. The push queue (and `ttp push`) rebase each approved head onto the branch tip, so a probe like
`git merge-base --is-ancestor <reviewed sha> origin/<branch>` never passes once they did.

- `landed:#<id>` (a start_when or retry_when) passes once task <id>'s landing is on the branch: the
  commit the push queue recorded for it (push_queue.landed_sha), a successful detached push, the
  commit a task recorded as superseding it (`ttp task <project> superseded <id> <sha>`), else (once
  it is done and committed something) its branch's commits by patch.
- `git merge-base --is-ancestor <sha> <ref>`, as a whole probe, also passes when every commit of
  <sha> has an equivalent on <ref>: the same `git patch-id --stable` (git cherry), the same author,
  author date and subject (a rebase keeps them; a settled conflict changes the patch), or a commit
  the push queue replayed from it (its trailer).

Exit codes, as a probe's: 0 landed, 1 not yet, 2 cannot tell (a bad id, sha or ref)."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import push
from .db import review_subject

LANDED_PROBE = re.compile(r"\s*landed:\s*#?(\d+)\s*")
ANCESTOR_PROBE = re.compile(r"\s*git\s+merge-base\s+--is-ancestor\s+([0-9a-f]{7,64})\s+([\w./@{}^~-]+)\s*")
REPLAYED = "Ttp-Replayed-From"   # batch.REPLAYED: a commit the push queue settled names the one it replays
FETCH_S = 40                     # within the daemon's 60 s probe timeout
NOT_YET, CANNOT = 1, 2


def probe_command(probe: str) -> str:
    """The shell command the daemon runs for a start_when or retry_when: `landed:#<id>` and a bare
    ancestor probe become `ttp landed`; anything else runs as written."""
    if m := LANDED_PROBE.fullmatch(probe or ""):
        return f"ttp landed --task {m.group(1)}"
    if m := ANCESTOR_PROBE.fullmatch(probe or ""):
        return f"ttp landed {m.group(1)} --onto {shlex.quote(m.group(2))}"
    return probe


def _git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _commit(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").stdout.strip()


def _key(line: str) -> tuple[str, str] | None:
    """(sha, author name/email/date/subject) of a `%H%x00%an%x00%ae%x00%at%x00%s` log line."""
    parts = line.split("\0")
    return (parts[0], "\0".join(parts[1:])) if len(parts) == 5 else None


def on_branch(repo: Path, sha: str, tip: str) -> bool:
    """Every commit of `sha` is on `tip`: `sha` is an ancestor, or each of its commits not on `tip`
    has an equivalent there (git cherry's patch-id, the same author, date and subject, or a replay
    trailer naming it). Version-bump commits on its top are left out, as the push queue drops them."""
    if _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode == 0:
        return True
    base = _git(repo, "merge-base", tip, sha).stdout.strip()
    if not base:
        return False
    top = sha
    while top != base and re.search(rf"^{push.BUMP_TRAILER}: ", _git(repo, "log", "-1", "--format=%B", top).stdout,
                                    re.M):
        top = _commit(repo, f"{top}^")
        if not top:
            return False
    if top == base:
        return True
    left = {ln.split()[1] for ln in _git(repo, "cherry", tip, top, base).stdout.splitlines()
            if ln.startswith("+ ") and len(ln.split()) == 2}
    if not left:
        return True
    fmt = "--format=%H%x00%an%x00%ae%x00%at%x00%s"
    mine = {k[0]: k[1] for ln in _git(repo, "log", "--no-merges", fmt, f"{base}..{top}").stdout.splitlines()
            if (k := _key(ln))}
    theirs = {k[1] for ln in _git(repo, "log", "--no-merges", fmt, f"{base}..{tip}").stdout.splitlines()
              if (k := _key(ln))}
    replayed = set(re.findall(rf"^{REPLAYED}: ([0-9a-f]{{40,64}})[ \t]*$",
                              _git(repo, "log", "--format=%B", f"{base}..{tip}").stdout, re.M))
    return all(c in replayed or mine.get(c) in theirs for c in left)


def _tip(repo: Path, onto: str, fetch: bool) -> tuple[str, str]:
    """(tip sha, ref name) of `onto` (a ref, remote/branch or branch of the repo's remotes), fetched
    first when asked and it names a remote branch. ("", why) when it cannot be read."""
    remotes = _git(repo, "remote").stdout.split()
    remote, _, branch = onto.partition("/")
    if onto.startswith("refs/remotes/"):
        remote, _, branch = onto[len("refs/remotes/"):].partition("/")
    if fetch and remote in remotes and branch:
        try:
            subprocess.run(["git", "-C", str(repo), "fetch", "-q", remote,
                            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=FETCH_S,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:
            pass
    tip = _commit(repo, onto)
    return (tip, onto) if tip else ("", f"cannot read {onto}")


def _push_ref(p, repo: Path) -> str:
    """The push branch to look on: <remote>/<branch>, or the local refs/heads/<branch> when the repo
    has no such remote (a remote-less repo pushes nowhere, so its branch is the local one)."""
    remote, branch = push.target(p, repo)
    if remote not in _git(repo, "remote").stdout.split():
        return f"refs/heads/{branch}"
    return f"{remote}/{branch}"


def record_push(p, marker: Path, m: dict) -> None:
    """Keep a successful shared-branch push independently of its run's outcome. The marker is
    written first; the daemon retries this insert on every tick, including after a restart."""
    if m.get("status") != "pushed" or m.get("own") or m.get("kind") == "batch":
        return
    task, sha, target = m.get("task"), m.get("sha"), m.get("target")
    if not (isinstance(task, (str, int)) and re.fullmatch(r"[0-9]+", str(task))
            and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha)
            and isinstance(target, str) and target):
        return
    if p.db.one("SELECT marker FROM task_landings WHERE marker=?", (marker.name,)):
        return
    if not p.db.task(int(task)):
        return
    p.db.x("INSERT INTO task_landings(marker, task, sha, target) VALUES(?,?,?,?) "
           "ON CONFLICT(marker) DO NOTHING", (marker.name, int(task), sha, target))


def reconcile_pushes(p, tid: int) -> None:
    """A worker can finish before the daemon tends its detached pushes, regardless of its handoff."""
    for marker in sorted((p.state / push.DETACHED).glob(f"t{tid}-*.json")):
        record_push(p, marker, push._read(marker))


def task_shas(p, tid: int) -> tuple[list[str], str]:
    """The commits that carry task `tid` to the branch, and where they come from: the landed commits
    the push queue recorded for it (its own approvals, those of a review of it, or of its branch),
    then its successful detached pushes, else, once it is done with commits of its own, its branch's
    head. ([], why) when there is none yet."""
    db = p.db
    task = db.task(tid)
    if not task:
        return [], f"no task #{tid}"
    branch = task.get("branch") or ""
    try:
        rows = db.q("SELECT q.task, q.branch, q.landed_sha, q.pushed_sha, t.labels, t.title FROM push_queue q "
                    "LEFT JOIN tasks t ON t.id=q.task WHERE q.status IN ('pushed','landed') ORDER BY q.id")
    except Exception:
        rows = []
    shas = [r["landed_sha"] or r["pushed_sha"] for r in rows
            if (r["landed_sha"] or r["pushed_sha"]) and (
                r["task"] == tid or (branch and r["branch"] == branch)
                or review_subject({"labels": r["labels"], "title": r["title"]}) == tid)]
    if shas:
        return shas, "its push queue landing"
    rows = db.q("SELECT l.task, l.sha, t.labels, t.title FROM task_landings l "
                "LEFT JOIN tasks t ON t.id=l.task ORDER BY l.marker")
    shas = list(dict.fromkeys(r["sha"] for r in rows if r["task"] == tid
                             or review_subject({"labels": r["labels"], "title": r["title"]}) == tid))
    if shas:
        return shas, "its detached push landing"
    shas = [r["sha"] for r in db.q("SELECT sha FROM task_superseded WHERE task=? ORDER BY ts", (tid,))]
    if shas:
        return shas, "the commit recorded as superseding it"
    head = _commit(p.root, f"refs/heads/{branch}") if branch else ""
    if not head:
        return [], f"#{tid} has no landing recorded and no branch"
    # The daemon makes the branch at the push branch's tip when the task starts: until the task is
    # done and has commits of its own, its head is "on the branch" with nothing landed.
    if task.get("status") != "done":
        return [], f"#{tid} has no landing recorded and has not finished ({task.get('status')})"
    if head == _start(p.root, branch):
        return [], f"#{tid} has no landing recorded and no commits on its branch {branch}"
    return [head], f"its branch {branch}"


def record_superseded(p, tid: int, sha: str) -> str:
    """Record that done task `tid`'s work landed on the push branch as `sha` (re-landed under another
    commit, e.g. after a push conflict), so the local-only check stops flagging its branch at the
    head it has now. Refused unless the task is done and `sha` is on the push branch, came after the
    branch left it (not the branch's base or an older commit) and changes a file the branch changes;
    ValueError says why."""
    task = p.db.task(tid)
    if not task:
        raise ValueError(f"no task #{tid}")
    if task.get("status") != "done":
        raise ValueError(f"not recorded: task #{tid} is {task.get('status')}, not done")
    branch = task.get("branch") or ""
    head = _commit(p.root, f"refs/heads/{branch}") if branch else ""
    if not head:
        raise ValueError(f"task #{tid} has no branch here to record")
    full = _commit(p.root, sha)
    if not full:
        raise ValueError(f"no commit {sha} in {p.root}")
    rc, words = check(p, p.root, full)
    if rc != 0:
        raise ValueError(f"not recorded: {words}")
    if _git(p.root, "merge-base", "--is-ancestor", full, head).returncode == 0:
        raise ValueError(f"not recorded: {full[:10]} is already in branch {branch}, so it predates the work")
    tip = _tip(p.root, _push_ref(p, p.root), False)[0]
    base = _git(p.root, "merge-base", head, tip).stdout.strip() if tip else ""
    if not base:
        raise ValueError(f"not recorded: branch {branch} shares no history with the push branch")
    theirs = set(_git(p.root, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "--root", "-m",
                      "--first-parent", "-z", full).stdout.split("\0")) - {""}
    mine = set(_git(p.root, "diff", "--name-only", "--no-renames", "-z", base, head).stdout.split("\0")) - {""}
    if not theirs & mine:
        raise ValueError(f"not recorded: {full[:10]} changes none of the files branch {branch} changes")
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
           (tid, head, full, time.time()))
    return f"task #{tid}: branch {branch} at {head[:10]} recorded as superseded by {full[:10]} ({words})"


def _keys(repo: Path, args: list[str], skip: list[str]) -> list[tuple[str, str]]:
    """[(commit, key)] of `git log <args>`, oldest first: the key is a hash of each file's added and
    removed lines (no context, no line numbers), with the `skip` paths left out. A rebase that moved
    the context or took the version files keeps the key; git's patch-id does not. Merges and commits
    that change only `skip` paths are left out."""
    out = _git(repo, "log", "--reverse", "--no-merges", "-p", "-U0", "--no-renames", "--no-color",
               "--format=%x00%H", *args, "--", ".", *(f":(exclude){x}" for x in skip), timeout=60)
    if out.returncode != 0:
        return []
    keys = []
    for chunk in out.stdout.split("\0")[1:]:
        sha, _, body = chunk.partition("\n")
        if key := _key_of(body):
            keys.append((sha.strip(), key))
    return keys


def _key_of(diff: str) -> str:
    """The key _keys gives a diff's changed lines; "" when it changes nothing."""
    files: dict[str, list[str]] = {}
    name, hunk = "", False
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            name, hunk = ln, False
            files.setdefault(name, [])
        elif ln.startswith("@@"):
            hunk = True
        elif hunk and ln[:1] in ("+", "-"):
            files[name].append(ln)
    files = {f: lines for f, lines in files.items() if lines}
    if not files:
        return ""
    return hashlib.sha256("\n".join(f"{f}\n" + "\n".join(lines) for f, lines in sorted(files.items()))
                          .encode()).hexdigest()


def equivalent(p, branch: str, bump=None) -> tuple[str, str] | None:
    """(commit on the push branch, branch head) when `branch`'s change already landed there under
    other commits: each of its own commits, or its whole change at once (a squash), has the same
    changed lines as a commit the push branch gained since the two parted, the version files and
    changesets of `bump` (delivery.version_bump) aside. None when it did not, the branch already is
    on the push branch (a plain landing) or has no change of its own, or it cannot be read.
    Local refs only: no fetch."""
    repo = p.root
    head = _commit(repo, f"refs/heads/{branch}")
    try:
        tip = _commit(repo, _push_ref(p, repo))
    except ValueError:
        return None
    if not head or not tip or _git(repo, "merge-base", "--is-ancestor", head, tip).returncode == 0:
        return None
    base = _git(repo, "merge-base", tip, head).stdout.strip()
    if not base:
        return None
    try:
        cfg = push.bump_of(bump)
    except ValueError:
        cfg = None
    skip = (cfg["files"] + ([cfg["changeset_dir"]] if cfg["changeset_dir"] else [])) if cfg else []
    mine = _keys(repo, [f"{base}..{head}"], skip)
    if not mine:
        return None
    theirs: dict[str, str] = {}
    for sha, key in _keys(repo, ["--max-count=500", f"{base}..{tip}"], skip):
        theirs.setdefault(key, sha)
    if all(key in theirs for _, key in mine):
        return theirs[mine[-1][1]], head
    whole = _git(repo, "diff", "-U0", "--no-renames", "--no-color", base, head, "--", ".",
                 *(f":(exclude){x}" for x in skip), timeout=60)
    key = _key_of(whole.stdout) if whole.returncode == 0 else ""
    return (theirs[key], head) if key in theirs else None


def superseded(db, tids) -> dict[int, list[tuple[str, str]]]:
    """{task: [(recorded head, sha)]} of record_superseded for tasks `tids`."""
    tids = [int(t) for t in tids]
    out: dict[int, list[tuple[str, str]]] = {}
    if tids:
        for r in db.q("SELECT task, head, sha FROM task_superseded WHERE task IN (%s)" % ",".join("?" * len(tids)),
                      tids):
            out.setdefault(r["task"], []).append((r["head"], r["sha"]))
    return out


def _start(repo: Path, branch: str) -> str:
    """The commit `branch` was created at (its oldest reflog entry), "" when git kept no reflog."""
    log = _git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}").stdout.split()
    return log[-1] if log else ""


def check(p, repo: Path, sha: str | None = None, tid: int | None = None, onto: str | None = None) -> tuple[int, str]:
    """(exit code, words) for `ttp landed`: a sha, or task `tid`'s landing, on `onto` (default the
    push branch). Reads the local ref first and fetches only when that says not yet."""
    try:
        onto = onto or _push_ref(p, repo)
    except ValueError as e:
        return CANNOT, str(e)
    if tid is not None:
        shas, src = task_shas(p, tid)
        if not shas:
            return NOT_YET, src
        what = f"#{tid} ({src})"
    else:
        full = _commit(repo, sha or "")
        if not full:
            return CANNOT, f"no commit {sha} in {repo}"
        shas, what = [full], (sha or "")[:10]
    for fetch in (False, True):
        tip, ref = _tip(repo, onto, fetch)
        if not tip:
            if fetch:
                return CANNOT, ref
            continue
        if any(_commit(repo, s) and on_branch(repo, s, tip) for s in shas):
            return 0, f"{what} is on {ref}"
    return NOT_YET, f"{what} is not on {onto} yet"


def run(p, args) -> int:
    tid = args.task
    if tid is None and args.ref and re.fullmatch(r"#\d+", args.ref):
        tid = int(args.ref[1:])
    if tid is None and not args.ref:
        print("ttp landed: give a commit or --task <id>")
        return CANNOT
    rc, words = check(p, p.root, None if tid is not None else args.ref, tid, args.onto)
    print(words)
    return rc
