# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-task archives of what a finished task's worktree held outside git, so the worktree can go.

state/archive/tasks/<id>/ holds manifest.json and the archived files: small ones in a compressed
tarball (files[.N].tar.gz), each over TAR_MAX_MB moved as it is under files/<path> (a rename on the
same filesystem, so archiving it needs no space). The manifest lists every file the sweep took out
of the worktree: path (relative to the worktree), size, sha256, whether a hand-off named it, and
where it went ("tar", "file", or "dropped": over the cap for unnamed files, listed only). It also
records the task's status, branch and head, so archived output is tied to the commit that made it,
and `patches`: binary patches of uncommitted tracked changes (tracked.patch, verified to apply to
that head; `git apply` restores them). state/archive/tasks/MANIFEST.md gets one line per archiving.

store() is transactional. It first checks that what it writes fits in the free space of state/'s
filesystem (see room); if not, it archives nothing and raises NoRoom. It then writes the manifest
with the new entries marked `pending`, writes the tarball, patch and big files, and finalizes the
manifest; only then are files deleted from the worktree. A failure part way undoes what it wrote
(temporary files, the tarball, partial copies, the patch; big files renamed out go back) and
re-raises, so the worktree is kept as it was. Known gap: a kill (power cut, watchdog) between moving
a big file and finalizing the manifest is not undone at once; the next sweep's recover() finishes or
rolls back the pending entries, so every file under files/ is listed and resolve() finds it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .project import Project, durable_append, durable_write, fsync_dir, write_json

ARCHIVE_DIR = "archive/tasks"
MANIFEST = "manifest.json"
TAR_MAX_MB = 256   # a file over this is moved into files/ as it is instead of into the tarball
RESERVE_FLOOR_MB = 1024   # free space an archive leaves while the disk is already below the guard (see room)
LOG_MAX_MB = 1     # ignored *.log files up to this size are archived with the rest (see worktree.sweep)


def task_dir(p: Project, task_id: int) -> Path:
    return p.state / ARCHIVE_DIR / str(int(task_id))


def manifest(p: Project, task_id: int) -> dict | None:
    """The task's archive manifest, or None when it has none."""
    try:
        data = json.loads((task_dir(p, task_id) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256(path: Path, progress=lambda: None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for n, block in enumerate(iter(lambda: fh.read(1 << 20), b""), 1):
            h.update(block)
            if n % 64 == 0:
                progress()
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


def tracked_patch(root: Path) -> bytes:
    """A binary patch of the worktree's uncommitted tracked changes against HEAD, checked to apply to
    HEAD in a scratch index (`git apply --check --cached`). Raises when there is none or it does not
    apply, so the worktree is then kept."""
    def git(*args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=300, **kw)
    diff = git("diff", "--binary", "HEAD")
    if diff.returncode != 0 or not diff.stdout.strip():
        raise RuntimeError("no patch of the tracked changes: " + (diff.stderr.decode(errors="replace")[:200]
                                                                  or "git diff found no change"))
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        (Path(tmp) / "changes.patch").write_bytes(diff.stdout)
        for args in (("read-tree", "HEAD"), ("apply", "--check", "--cached", str(Path(tmp) / "changes.patch"))):
            r = git(*args, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"the patch of the tracked changes does not apply to HEAD: "
                                   f"{r.stderr.decode(errors='replace')[:200]}")
    return diff.stdout


class NoRoom(Exception):
    """An archive does not fit in the free space of state/'s filesystem; nothing was archived."""


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def need_bytes(root: Path, dest: Path, small: list[str], big: list[str], sizes: dict, patch: bytes | None) -> int:
    """Bytes an archive writes on state/'s filesystem: the tarball's inputs (with header room), the
    patch, the manifest, and the big files unless a rename moves them there (the same filesystem)."""
    out = sum(sizes[f] + 1024 for f in small) + len(patch or b"") + 64 * 1024 + 512 * (len(small) + len(big))
    if big and not _same_fs(root, dest):
        out += sum(sizes[f] for f in big)
    return out


def room(dest: Path, need: int, reserve: float) -> str | None:
    """Why `need` bytes do not fit under `dest` leaving `reserve` (the disk guard's threshold) free, or
    None when they fit. While free space is already below `reserve` (the disk guard's own sweep, which
    frees far more than it archives), RESERVE_FLOOR_MB must stay free instead."""
    try:
        free = shutil.disk_usage(dest).free
    except OSError as e:
        return f"archiving skipped: cannot read the free space under {dest} ({e}); the worktree is kept"
    keep = reserve if free >= reserve else min(reserve, RESERVE_FLOOR_MB * 1e6)
    if need <= free - keep:
        return None
    return (f"archiving skipped: it needs {need / 1e6:.1f} MB under {dest}, {free / 1e6:.1f} MB are free and "
            f"{keep / 1e6:.1f} MB must stay free; nothing archived, the worktree is kept")


def recover(p: Project, task_id: int) -> int:
    """Finish or roll back an archiving cut off part way (a kill between writing and finalizing):
    a pending entry whose data is in place (the tarball it names, its file under files/ at its size,
    a patch with its sha256) is finalized, the rest are dropped with their partial files, and
    temporary files are removed. Returns how many pending entries it found. Run under prune.lock."""
    m = manifest(p, task_id)
    dest = task_dir(p, task_id)
    for tmp in (list(dest.glob("*.tmp")) + list(dest.glob(".*.tmp"))) if dest.is_dir() else []:
        tmp.unlink(missing_ok=True)
    if not m:
        return 0
    files = [e for e in m.get("files") or [] if isinstance(e, dict)]
    patches = [e for e in m.get("patches") or [] if isinstance(e, dict)]
    pending = [e for e in files + patches if e.get("pending")]
    if not pending:
        return 0

    def present(e: dict) -> bool:
        if "file" in e and "paths" in e:   # a patch
            f = dest / str(e["file"])
            return f.is_file() and f.stat().st_size == e.get("size") and sha256(f) == e.get("sha256")
        if e.get("stored") == "tar":
            return (dest / str(e.get("archive"))).is_file()
        if e.get("stored") == "file":
            f = dest / "files" / str(e["path"])
            f.with_name(f.name + ".part").unlink(missing_ok=True)
            if f.is_symlink() or (f.is_file() and f.stat().st_size == e.get("size")):
                return True
            if f.exists() or f.is_symlink():
                f.unlink()   # a partial copy
            return False
        return False   # "dropped": the files were never deleted before finalizing
    kept_files = [{k: v for k, v in e.items() if k != "pending"} for e in files
                  if not e.get("pending") or present(e)]
    kept_patches = [{k: v for k, v in e.items() if k != "pending"} for e in patches
                    if not e.get("pending") or present(e)]
    for e in patches:
        if e.get("pending") and not present(e):
            (dest / str(e["file"])).unlink(missing_ok=True)
    write_json(dest / MANIFEST, {**m, "updated": time.time(), "files": kept_files, "patches": kept_patches})
    return len(pending)


def store(p: Project, task: dict, root: Path, named: list[str], others: list[str], cap_mb: float,
          head: str = "", patch: bytes | None = None, tracked: list[str] = (), reserve_bytes: float = 0,
          progress=lambda: None) -> dict:
    """Archive files of the worktree `root` before it is removed. `named` (entries a hand-off lists)
    are always kept. `others` (untracked files no hand-off names) are listed with size and sha256,
    and kept smallest first while they total at most `cap_mb`; the rest are dropped (listed only).
    Transactional (see the module docstring): raises NoRoom, writing nothing, when the archive does
    not fit with `reserve_bytes` left free (see room); the manifest is written with the new entries
    pending before anything leaves the worktree, and finalized once all is written; a file is deleted
    from the worktree only after that; a failure undoes what it wrote and re-raises. An earlier
    archiving cut off part way is recovered first (recover); archiving again adds to the manifest.
    `patch` (tracked_patch, for the `tracked` paths) is saved as tracked[.N].patch. `progress()` is
    called per file (and per 64 MB hashed) so a daemon's watchdog sees it move.
    Returns {"dir", "files", "bytes" (archived), "dropped", "dropped_bytes"}."""
    dest = task_dir(p, task["id"])
    dest.mkdir(parents=True, exist_ok=True)
    recover(p, task["id"])
    old = manifest(p, task["id"]) or {}
    entries = [e for e in old.get("files") or [] if isinstance(e, dict)]
    patches = [e for e in old.get("patches") or [] if isinstance(e, dict)]
    named_files, keep, dropped, sizes = _split(root, named, others, cap_mb)
    big = [f for f in keep if sizes[f] > TAR_MAX_MB * 1e6 and not (root / f).is_symlink()]
    small = [f for f in keep if f not in big]
    why = room(dest, need_bytes(root, dest, small, big, sizes, patch), reserve_bytes)
    if why:
        raise NoRoom(why)
    tar_name = patch_name = ""
    if small:
        n = 0
        while (dest / (tar_name := "files.tar.gz" if not n else f"files.{n}.tar.gz")).exists():
            n += 1
    if patch:
        n = 0
        while (dest / (patch_name := "tracked.patch" if not n else f"tracked.{n}.patch")).exists():
            n += 1
    new = []
    for f in keep + dropped:
        full = root / f
        progress()
        new.append({"path": f, "size": sizes[f], "sha256": "" if full.is_symlink() else sha256(full, progress),
                    "named": f in named_files,
                    "stored": "dropped" if f in dropped else "file" if f in big else "tar",
                    **({"archive": tar_name} if f in small else {}), "at": time.time()})
    new_patches = [{"file": patch_name, "size": len(patch), "sha256": hashlib.sha256(patch).hexdigest(),
                    "paths": list(tracked), "at": time.time()}] if patch else []
    info = {"task": task["id"], "status": task.get("status") or "", "branch": task.get("branch") or "",
            "head": head or old.get("head") or "", "worktree": str(root)}
    write_json(dest / MANIFEST, {**info, "updated": time.time(), "files": entries + [{**e, "pending": True} for e in new],
                                  "patches": patches + [{**e, "pending": True} for e in new_patches]})
    made: list[Path] = []          # files this call wrote under dest, removed on failure
    moved: list[tuple[Path, Path]] = []   # big files renamed out of the worktree, renamed back on failure
    copied: list[str] = []         # big files copied (another filesystem): deleted from the worktree after
    done = False
    try:
        if patch:
            made.append(dest / patch_name)
            durable_write(dest / patch_name, patch)
        if small:
            tmp = dest / (tar_name + ".tmp")
            made.append(tmp)
            with tarfile.open(tmp, "w:gz", compresslevel=1) as tar:
                for f in small:
                    progress()
                    tar.add(str(root / f), arcname=f, recursive=False)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            made.append(dest / tar_name)
            os.replace(tmp, dest / tar_name)
            fsync_dir(dest)
        for f in big:
            progress()
            to = dest / "files" / f
            to.parent.mkdir(parents=True, exist_ok=True)
            if _same_fs(root / f, to.parent):
                os.rename(root / f, to)
                moved.append((to, root / f))
            else:
                part = to.with_name(to.name + ".part")
                made += [part] + ([] if to.exists() else [to])
                shutil.copy2(root / f, part)
                with open(part, "rb") as fh:
                    os.fsync(fh.fileno())
                os.replace(part, to)
                copied.append(f)
            fsync_dir(to.parent)
        write_json(dest / MANIFEST, {**info, "updated": time.time(), "files": entries + new,
                                      "patches": patches + new_patches})
        done = True
    finally:
        if not done:
            for to, back in reversed(moved):
                try:
                    os.rename(to, back)
                except OSError:
                    pass   # left pending: recover() finalizes it, so the file stays listed and findable
            for f in made:
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                recover(p, task["id"])
            except Exception:
                pass   # the next sweep recovers it
    line = (f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime())} task {task['id']} ({task.get('status') or '?'}), "
            f"branch {task.get('branch') or '-'}, head {(head or '-')[:12]}: {len(keep)} file(s) "
            f"({sum(sizes[f] for f in keep) / 1e6:.1f} MB) archived, {len(dropped)} listed only"
            + (f", a patch of {len(tracked)} tracked file(s)" if patch else "") + "\n")
    durable_append(p.state / ARCHIVE_DIR / "MANIFEST.md", line)
    for f in small + dropped + copied:
        try:
            (root / f).unlink()
        except FileNotFoundError:
            pass
    return {"dir": str(dest), "files": len(keep), "bytes": sum(sizes[f] for f in keep),
            "dropped": len(dropped), "dropped_bytes": sum(sizes[f] for f in dropped),
            **({"patch": str(dest / patch_name)} if patch else {})}


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


def plan(root: Path, named: list[str], others: list[str], cap_mb: float, patch: bytes | None = None) -> dict:
    """What store() would archive and drop, changing nothing (for a dry run)."""
    _, keep, dropped, sizes = _split(root, named, others, cap_mb)
    return {"files": len(keep), "bytes": sum(sizes[f] for f in keep), "dropped": len(dropped),
            "dropped_bytes": sum(sizes[f] for f in dropped), **({"patch": "(dry run)"} if patch else {})}


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
