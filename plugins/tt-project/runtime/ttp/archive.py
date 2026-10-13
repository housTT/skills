# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-task archives of what a finished task's worktree held outside git, so the worktree can go.

state/archive/tasks/<id>/ holds manifest.json and the archived files: small ones in a compressed
tarball (files[.N].tar.gz), each over TAR_MAX_MB moved as it is under files/<path> (a rename on the
same filesystem, so archiving it needs no space). The manifest lists every file the sweep took out
of the worktree: path (relative to the worktree), size, sha256, whether a hand-off named it, and
where it went ("tar", "file", or "dropped": over the cap for unnamed files, listed only). It also
records the task's branch and head, so archived output is tied to the commit that made it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

from .project import Project

ARCHIVE_DIR = "archive/tasks"
MANIFEST = "manifest.json"
TAR_MAX_MB = 256   # a file over this is moved into files/ as it is instead of into the tarball


def task_dir(p: Project, task_id: int) -> Path:
    return p.state / ARCHIVE_DIR / str(int(task_id))


def manifest(p: Project, task_id: int) -> dict | None:
    """The task's archive manifest, or None when it has none."""
    try:
        data = json.loads((task_dir(p, task_id) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def files_of(root: Path, rels: list[str]) -> list[str]:
    """The regular files and links these entries (files or directories, relative to `root`) hold,
    relative to it, links not followed."""
    out: list[str] = []
    for rel in rels:
        full = root / rel
        if full.is_dir() and not full.is_symlink():
            out += sorted(f.relative_to(root).as_posix() for f in full.rglob("*")
                          if f.is_symlink() or f.is_file())
        elif full.is_symlink() or full.is_file():
            out.append(rel)
    return list(dict.fromkeys(out))


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def store(p: Project, task: dict, root: Path, named: list[str], others: list[str], cap_mb: float,
          head: str = "") -> dict:
    """Archive files of the worktree `root` before it is removed. `named` (entries a hand-off lists)
    are always kept. `others` (untracked files no hand-off names) are listed with size and sha256,
    and kept smallest first while they total at most `cap_mb`; the rest are dropped (listed only).
    The manifest is written before anything leaves the worktree; a file is deleted there only once it
    is in the archive. Archiving again (an earlier sweep stopped half way) adds to the manifest.
    Returns {"dir", "files", "bytes" (archived), "dropped", "dropped_bytes"}."""
    dest = task_dir(p, task["id"])
    dest.mkdir(parents=True, exist_ok=True)
    old = manifest(p, task["id"]) or {}
    entries = [e for e in old.get("files") or [] if isinstance(e, dict)]
    named_files, keep, dropped, sizes = _split(root, named, others, cap_mb)
    big = [f for f in keep if sizes[f] > TAR_MAX_MB * 1e6 and not (root / f).is_symlink()]
    small = [f for f in keep if f not in big]
    tar_name = ""
    if small:
        n = 0
        while (dest / (tar_name := "files.tar.gz" if not n else f"files.{n}.tar.gz")).exists():
            n += 1
    new = []
    for f in keep + dropped:
        full = root / f
        new.append({"path": f, "size": sizes[f], "sha256": "" if full.is_symlink() else sha256(full),
                    "named": f in named_files,
                    "stored": "dropped" if f in dropped else "file" if f in big else "tar",
                    **({"archive": tar_name} if f in small else {}), "at": time.time()})
    if small:
        tmp = dest / (tar_name + ".tmp")
        with tarfile.open(tmp, "w:gz", compresslevel=1) as tar:
            for f in small:
                tar.add(str(root / f), arcname=f, recursive=False)
        os.replace(tmp, dest / tar_name)
    for f in big:
        to = dest / "files" / f
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / f), str(to))
    _write_json(dest / MANIFEST, {"task": task["id"], "branch": task.get("branch") or "",
                                  "head": head or old.get("head") or "", "worktree": str(root),
                                  "updated": time.time(), "files": entries + new})
    for f in small + dropped:
        try:
            (root / f).unlink()
        except FileNotFoundError:
            pass
    return {"dir": str(dest), "files": len(keep), "bytes": sum(sizes[f] for f in keep),
            "dropped": len(dropped), "dropped_bytes": sum(sizes[f] for f in dropped)}


def _split(root: Path, named: list[str], others: list[str], cap_mb: float):
    """(the named files, the files to archive, the files to drop, size of each); see store."""
    named_files = files_of(root, named)
    other_files = [f for f in files_of(root, others) if f not in named_files]
    sizes = {f: (root / f).lstat().st_size for f in named_files + other_files}
    keep, left, dropped = list(named_files), cap_mb * 1e6, []
    for f in sorted(other_files, key=lambda f: (sizes[f], f)):
        if sizes[f] <= left:
            keep.append(f)
            left -= sizes[f]
        else:
            dropped.append(f)
    return named_files, keep, dropped, sizes


def plan(root: Path, named: list[str], others: list[str], cap_mb: float) -> dict:
    """What store() would archive and drop, changing nothing (for a dry run)."""
    _, keep, dropped, sizes = _split(root, named, others, cap_mb)
    return {"files": len(keep), "bytes": sum(sizes[f] for f in keep), "dropped": len(dropped),
            "dropped_bytes": sum(sizes[f] for f in dropped)}


def resolve(p: Project, entry: str, task_id: int | None = None) -> dict | None:
    """Where an archived artifact is now: `entry` is a path inside a removed task worktree (absolute,
    or relative to it when `task_id` is given). Returns its manifest entry with `location` (the file
    under files/, or `<tarball>:<member>`), or None when no archive holds it."""
    raw = Path(entry.strip())
    if raw.is_absolute():
        try:
            rel = raw.relative_to(p.worktrees)
        except ValueError:
            return None
        head, *rest = rel.parts
        if not head.startswith("t") or not head[1:].isdigit() or not rest:
            return None
        task_id, path = int(head[1:]), Path(*rest).as_posix()
    elif task_id is None:
        return None
    else:
        path = raw.as_posix()
    m = manifest(p, task_id)
    if not m:
        return None
    for e in reversed(m.get("files") or []):
        if e.get("path") == path or str(e.get("path", "")).startswith(path.rstrip("/") + "/"):
            d = task_dir(p, task_id)
            loc = {"tar": f"{d / str(e.get('archive'))}:{e['path']}", "file": str(d / "files" / e["path"])}
            return {**e, "task": task_id, "location": loc.get(e.get("stored"), "")}
    return None


def restore(p: Project, task_id: int, dest: Path) -> list[str]:
    """Extract every archived file of the task under `dest` (relative paths kept). Returns the paths."""
    m = manifest(p, task_id) or {}
    d = task_dir(p, task_id)
    out = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted({e["archive"] for e in m.get("files") or [] if e.get("stored") == "tar"}):
        with tarfile.open(d / name, "r:gz") as tar:
            members = [x for x in tar.getmembers() if (x.isfile() or x.issym()) and not x.name.startswith("/")
                       and ".." not in Path(x.name).parts]
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(dest, members=members, **safe)
            out += [x.name for x in members]
    for e in m.get("files") or []:
        if e.get("stored") == "file" and (d / "files" / e["path"]).exists():
            to = dest / e["path"]
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / "files" / e["path"], to)
            out.append(e["path"])
    return out
