# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Parallel code tasks that build the same files: a model-free warning, never a refusal.

Two code tasks run side by side and each adds its own copy of one new module: the APIs clash and
a review fails. So when a `code` or `harness` task is added, the repo paths its spec names
(spec_paths: path-like tokens with a known source extension, or whose folder is in the repo) are
compared with (a) the paths other open code tasks' specs name and (b) the files that finished,
not yet landed code branches add or change (`git diff --name-status <base>...<head>`, cached per
head). An overlap gives one short line (warning) in task_add's result and the new task's line in
the digest's open tasks, naming the other task, the shared paths (new files flagged) and the fix:
`start_when landed:#<id>` or `depends_on`. A code worker's prompt lists the unlanded branches
that touch what its spec names (prompt_block). Common files never count: the `overlap.ignore`
globs (changelogs, changesets, manifests, `__init__.py`) and hubs, tracked files larger than
`overlap.hub_bytes`, which most changes touch in passing. No network, no model.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import settled

KINDS = ("code", "harness")
OPEN = ("queued", "running", "review", "pushing", "waiting", "blocked")
LABEL = "overlap:"           # a task's label: overlap:<ids>:<warning>, shown in the digest while queued
CACHE = "overlap_diffs.json"  # state/: {head: [[status, path], ...]}, one git diff per head ever
CACHE_MAX = 400
MAX_PATHS = 4                 # shared paths named in one warning
MAX_TASKS = 3                 # other tasks named in one warning
LS_TTL_S = 60
# A slash path without a known extension (bin/tool, skills/tt-x/SKILL): kept when its folder is in the repo.
_PLAIN = re.compile(r"(?<![\w./:@-])((?:[\w.-]+/)+[\w-][\w.-]*)(?![\w/-])")
_ls_cache: dict[str, tuple[float, list[str]]] = {}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=20,
                          stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def settings(p) -> dict:
    return (p.config().get("overlap") or {}) if p is not None else {}


def tracked(root: Path) -> list[str]:
    """The repo's tracked files (git ls-files), kept LS_TTL_S per root; [] outside git."""
    key, now = str(root), time.time()
    hit = _ls_cache.get(key)
    if hit and now - hit[0] < LS_TTL_S:
        return hit[1]
    try:
        out = _git(root, "ls-files").stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        out = []
    _ls_cache[key] = (now, out)
    return out


def same(a: str, b: str) -> bool:
    """Two repo paths name one file: equal, or one is the other below a folder (a spec may name
    runtime/x.py for plugins/tool/runtime/x.py)."""
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _dirs(files: list[str]) -> set[str]:
    out = set()
    for f in files:
        parts = f.split("/")[:-1]
        out.update("/".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1))
    return out


def spec_paths(text: str, root: Path) -> list[str]:
    """The repo paths `text` names: slash paths with a known source extension, or whose folder the
    repo has. Bare file names (named in passing) and URLs are left out."""
    text = re.sub(r"\b[a-z]+://\S+", " ", text or "")
    known = [f for f in settled.files_in(text) if "/" in f]
    plain = [t for t in _PLAIN.findall(text) if t not in known and "." not in t.rsplit("/", 1)[-1]]
    if plain:
        dirs = _dirs(tracked(root))
        plain = [t for t in plain if t.rsplit("/", 1)[0] in dirs]
    return list(dict.fromkeys(f.removeprefix("./") for f in known + plain))


def _ignored(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, "*/" + g) for g in globs or ())


def _hub(path: str, root: Path, hub_bytes: int) -> bool:
    """A tracked file this path names is larger than hub_bytes: most changes touch it in passing."""
    if hub_bytes <= 0:
        return False
    for f in tracked(root):
        if same(f, path):
            try:
                if (root / f).stat().st_size > hub_bytes:
                    return True
            except OSError:
                pass
    return False


def keep(p, paths) -> list[str]:
    """`paths` without ignored files and hubs."""
    s = settings(p)
    hub = int(s.get("hub_bytes") or 0)
    return [f for f in paths if not _ignored(f, s.get("ignore")) and not _hub(f, p.root, hub)]


def _cache_path(p) -> Path:
    return p.state / CACHE


def _load_cache(p) -> dict:
    try:
        got = json.loads(_cache_path(p).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def diff(p, base: str, head: str, cache: dict) -> list[tuple[str, str]]:
    """[(A|M|D, path)] of `base...head`, from `cache` (keyed by head) or one git diff."""
    if head in cache:
        return [tuple(x) for x in cache[head]]
    r = _git(p.root, "diff", "--name-status", "--no-renames", f"{base}...{head}")
    if r.returncode != 0:
        return []
    out = [(ln[0], ln.split("\t", 1)[1]) for ln in r.stdout.splitlines() if "\t" in ln]
    cache[head] = out
    return out


def _landed_ids(db) -> set[int]:
    """Tasks the records show landed or superseded, and the subjects of reviews that landed."""
    from .db import review_subject
    ids = {r["task"] for r in db.q("SELECT task FROM task_landings UNION SELECT task FROM task_superseded")}
    for r in db.q("SELECT id, labels, title FROM tasks WHERE kind='review' AND id IN "
                  "(SELECT task FROM task_landings)"):
        if (s := review_subject(dict(r))) is not None:
            ids.add(s)
    return ids


def branches(p, skip=()) -> list[dict]:
    """Code tasks' unlanded branches with commits of their own: [{id, branch, head, files: [(A|M|D, path)]}].
    Done or open tasks of the last `overlap.branch_days`, not landed by the records nor merged into
    the base; a diff per head is read once and cached in state/."""
    from . import worktree
    days = float(settings(p).get("branch_days") or 14)
    rows = p.db.q("SELECT id, branch FROM tasks WHERE kind IN (%s) AND branch IS NOT NULL AND branch != '' "
                  "AND status NOT IN ('failed','cancelled') AND updated>? ORDER BY id"
                  % ",".join("?" * len(KINDS)), (*KINDS, time.time() - days * 86400))
    landed = _landed_ids(p.db)
    rows = [r for r in rows if r["id"] not in landed and r["id"] not in skip]
    if not rows:
        return []
    try:
        base = worktree.resolve_base(p)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return []
    refs = [f"refs/heads/{r['branch']}" for r in rows]
    heads = dict(ln.split(" ", 1)[::-1] for ln in _git(p.root, "for-each-ref", "--no-merged", base,
                                                       "--format=%(objectname) %(refname:short)", *refs
                                                       ).stdout.splitlines() if " " in ln)
    cache = _load_cache(p)
    size = len(cache)
    out = []
    for r in rows:
        head = heads.get(r["branch"])
        if head and (files := diff(p, base, head, cache)):
            out.append({"id": r["id"], "branch": r["branch"], "head": head, "files": files})
    if len(cache) != size:
        keepers = dict(list(cache.items())[-CACHE_MAX:])
        try:
            tmp = _cache_path(p).with_suffix(".tmp")
            tmp.write_text(json.dumps(keepers))
            tmp.replace(_cache_path(p))
        except OSError:
            pass
    return out


def find(p, spec: str, skip=()) -> list[dict]:
    """The other tasks whose spec or unlanded branch names a path `spec` names:
    [{id, paths: [(path, new)], branch?, head?}], ignored files and hubs left out."""
    if not settings(p).get("enabled", True):
        return []
    mine = keep(p, spec_paths(spec, p.root))
    if not mine:
        return []
    hits: dict[int, dict] = {}
    open_rows = p.db.q("SELECT id, spec FROM tasks WHERE kind IN (%s) AND status IN (%s) ORDER BY id"
                       % (",".join("?" * len(KINDS)), ",".join("?" * len(OPEN))), (*KINDS, *OPEN))
    for r in open_rows:
        if r["id"] in skip:
            continue
        theirs = spec_paths(r["spec"] or "", p.root)
        shared = [m for m in mine if any(same(m, t) for t in theirs)]
        if shared:
            hits[r["id"]] = {"id": r["id"], "paths": {m: False for m in shared}}
    for b in branches(p, skip):
        shared = {}
        for status, f in b["files"]:
            for m in mine:
                if same(m, f):
                    shared[m] = shared.get(m, False) or status == "A"
        if shared:
            h = hits.setdefault(b["id"], {"id": b["id"], "paths": {}})
            for m, new in shared.items():
                h["paths"][m] = h["paths"].get(m, False) or new
            h.update(branch=b["branch"], head=b["head"])
    return [{**h, "paths": list(h["paths"].items())} for _, h in sorted(hits.items())]


def warning(hits: list[dict]) -> str:
    """One line naming the other tasks, the shared paths (new files flagged) and the fix."""
    if not hits:
        return ""
    parts = []
    for h in hits[:MAX_TASKS]:
        names = [f"{f} (new file)" if new else f for f, new in h["paths"][:MAX_PATHS]]
        more = len(h["paths"]) - MAX_PATHS
        parts.append(f"#{h['id']} ({', '.join(names)}{f', +{more} more' if more > 0 else ''})")
    more = len(hits) - MAX_TASKS
    first = hits[0]["id"]
    return (f"overlaps {'; '.join(parts)}{f'; +{more} more tasks' if more > 0 else ''}: build on it, not beside "
            f"it: start_when landed:#{first} or depends_on [{first}]")


def label(hits: list[dict]) -> str:
    return f"{LABEL}{','.join(str(h['id']) for h in hits)}:{warning(hits)}" if hits else ""


def digest_note(task: dict) -> str:
    """The overlap warning a queued task carries, '' once it waits on the task(s) it names or has started."""
    if task.get("status") != "queued":
        return ""
    from .db import deferral, dependency_ids
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(LABEL):
            ids_s, _, text = lb[len(LABEL):].partition(":")
            ids = {int(x) for x in ids_s.split(",") if x.isdigit()}
            when = deferral(task).get("when") or ""
            waits = set(dependency_ids(task)) | {int(x) for x in re.findall(r"#(\d+)", when)}
            return "" if ids and ids <= waits else text
    return ""


def prompt_block(p, task: dict) -> str:
    """A code worker's block: the other unlanded branches that add or change a path its spec names."""
    if (task.get("kind") or "") not in KINDS:
        return ""
    try:
        mine = keep(p, spec_paths(task.get("spec") or "", p.root)) if settings(p).get("enabled", True) else []
        if not mine:
            return ""
        lines = []
        for b in branches(p, skip={int(task.get("id") or 0)}):
            shared = [(f, s == "A") for s, f in b["files"] if any(same(m, f) for m in mine)]
            if shared:
                names = ", ".join(f"{f} (new file)" if new else f for f, new in shared[:MAX_PATHS])
                lines.append(f"- #{b['id']} {b['branch']} at {b['head'][:10]}: {names}")
    except Exception:   # a hint, never a reason to fail a dispatch
        return ""
    if not lines:
        return ""
    return ("## Unlanded branches that touch your files\n"
            "These other tasks' branches add or change files your spec names and have not landed yet. Build on "
            "or coordinate with them: reuse their module and API (read it with `git show <branch>:<path>`), "
            "never create a second one beside it.\n" + "\n".join(lines))
