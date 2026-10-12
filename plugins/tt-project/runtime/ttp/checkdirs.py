# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""State-owned check outputs and temporary storage; never follow directory or lock links.

Managed scratch is only for check processes holding the lifetime guard. Supervisors and
non-check commands (including after_push) keep their ordinary temporary environment.
Legacy allocations without owner.json and old run-local scratch are left untouched, with
no migration. Only owned, unlocked check allocations are reclaimed; nothing else is.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

FOLDER = 'check-runs'
SCRATCH, LOCK = 'checks-tmp', 'checks-tmp.lock'
OWNER = 'owner.json'


@contextmanager
def directory(value, *, create=False):
    """Open every component without following links, keeping the directory pinned for operations."""
    if not value or not Path(value).is_absolute() or len(Path(value).parts) == 1 or '..' in Path(value).parts:
        raise OSError('TTP_RUN_DIR must be a non-root absolute directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(value).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as e:
        raise OSError(f'TTP_RUN_DIR directory unavailable or unsafe: {value}: {e}') from e
    finally:
        os.close(fd)


def writable(value, *, create=False) -> str:
    """Fail before launching a check if its output directory is missing, linked or unwritable."""
    with directory(value, create=create) as fd:
        name = '.write-probe-' + uuid.uuid4().hex
        probe = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.write(probe, b'check output\n')
        finally:
            os.close(probe)
            os.unlink(name, dir_fd=fd)
    return str(value)


def new(state: Path, owner: Path | None = None) -> Path:
    from .runner import boot_id, proc_start
    folder = state / FOLDER
    with directory(folder, create=True) as fd:
        # Publish the creator identity before recovery can inspect this allocation.
        with _take_fd(fd, wait=True):
            name = uuid.uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=fd)
            with directory(folder / name) as child:
                out = os.open(OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=child)
                with os.fdopen(out, 'w') as f:
                    json.dump({'marker': str(owner) if owner else None, 'pid': os.getpid(),
                               'started': proc_start(os.getpid()), 'boot': boot_id()}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.fsync(child)
            os.fsync(fd)
    return folder / name


def reports(path: Path) -> Path:
    """Stable artifacts live apart from the disposable allocation and its temporary files."""
    return path.parent.parent / 'check-reports' / path.name


def environment(path: Path, env: dict) -> dict:
    writable(path)
    scratch = path / SCRATCH
    writable(scratch, create=True)
    output = writable(reports(path), create=True)
    env = {k: v for k, v in env.items() if k not in ('TTP_PIDNS', 'TTP_CGROUP')}
    env.update(TTP_RUN_DIR=output, TTP_CHECK_OUTPUT_DIR=output)
    return env


def for_check(env: dict) -> dict:
    """Assign managed scratch only at the guarded check subprocess boundary."""
    if env.get('TTP_CHECKS_TMP_FD') and env.get('TTP_CHECK_OUTPUT_DIR'):
        output = Path(env['TTP_CHECK_OUTPUT_DIR'])
        folder = output.parent.parent / FOLDER
        scratch = writable(folder / output.name / SCRATCH)
        env.update({k: scratch for k in ('TMPDIR', 'TMP', 'TEMP')})
        # Preserve user ceilings; this one only bounds discovery beneath check-runs.
        inherited = env.get('GIT_CEILING_DIRECTORIES', '')
        env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(filter(None, (inherited, str(folder))))
    return env


def inherited() -> tuple[int, ...]:
    value = os.environ.get('TTP_CHECKS_TMP_FD')
    return (int(value),) if value is not None else ()


def finish(path: Path) -> None:
    """The owner has durably recorded its outcome; descendants still keep their lifetime lock."""
    if not _allocation(path):
        return
    try:
        with directory(path) as fd:
            if not _read(fd, OWNER):
                return
            done = os.open('finished', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
            os.close(done)
            os.fsync(fd)
    except OSError:
        pass
    # Another owner may be publishing or removing an allocation. Completion must not
    # abandon its cleanup just because that short catalogue operation is in flight.
    cleanup(path, whole=True, wait=True)


def release(path: Path) -> None:
    for fd in inherited():
        os.close(fd)
    os.environ.pop('TTP_CHECKS_TMP_FD', None)
    finish(path)


def restore(state: Path, value) -> Path:
    """Only rebuild a recorded direct child of this project's check-runs directory."""
    folder = state / FOLDER
    if not isinstance(value, str) or Path(value).parent != folder or len(Path(value).name) != 32 or \
            any(c not in '0123456789abcdef' for c in Path(value).name):
        raise OSError('TTP_RUN_DIR has no valid state-owned check output directory in its queue record')
    writable(value, create=True)
    return Path(value)


def take(path: Path):
    """A lifetime flock with no writes: links, devices and multiply-linked files are refused."""
    with directory(path) as fd:
        return _take_fd(fd)


def _take_fd(fd, *, wait=False):
    lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    f = os.fdopen(lock_fd, 'r+')
    try:
        st = os.fstat(lock_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError('checks lifetime lock must be a single-link regular file')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            f.close()
            return None
        return f
    except BaseException:
        f.close()
        raise


def _remove(parent: int, name: str) -> None:
    """Remove only through pinned directory descriptors (also on Python 3.9)."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        for entry in os.listdir(fd):
            if stat.S_ISDIR(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode):
                _remove(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _read(fd: int, name: str) -> dict:
    try:
        raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(raw) as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return {}
            data = json.loads(f.read(65536))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _allocation(path: Path) -> bool:
    return (path.is_absolute() and path.parent.name == FOLDER and len(path.name) == 32
            and all(c in '0123456789abcdef' for c in path.name))


def _reserved(fd: int, path: Path) -> bool:
    from .runner import boot_id, proc_start
    try:
        os.stat('finished', dir_fd=fd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        pass
    owner = _read(fd, OWNER)
    if marker := owner.get('marker'):
        try:
            with directory(Path(marker).parent) as parent:
                m = _read(parent, Path(marker).name)
        except OSError:
            m = {}
        if m.get('check_run_dir') == str(path):
            # Queued pushes have no process/lock yet, including after a restart.
            # Running owners are protected by the inherited lifetime lock.
            if m.get('status') == 'queued':
                return True
            if not (m.get('status') == 'running' and not m.get('pid')):
                return False
    # Covers the interval before the launcher writes the marker, and foreground pushes.
    return bool(owner.get('pid') and owner.get('started') and owner.get('boot') == boot_id()
                and proc_start(owner['pid']) == owner['started'])


def cleanup(path: Path, *, whole=False, wait=False) -> None:
    if not whole or not _allocation(path):
        return
    try:
        with directory(path.parent) as parent:
            # Serialize allocation publication and removal; never follow a renamed path.
            catalogue = _take_fd(parent, wait=wait)
            if catalogue is None:
                return
            try:
                with directory(path) as fd:
                    if not _read(fd, OWNER):
                        return
                    guard = _take_fd(fd)
                    if guard is None:
                        return
                    try:
                        if not _reserved(fd, path):
                            _remove(parent, path.name)
                            try:
                                with directory(reports(path).parent) as report_fd:
                                    os.rmdir(path.name, dir_fd=report_fd)  # keep actual reports
                            except OSError:
                                pass
                    finally:
                        guard.close()
            finally:
                catalogue.close()
    except OSError:
        pass   # unsafe, busy or already gone; recovery must never follow it elsewhere


def recover(state: Path, runs: Path) -> None:
    try:
        with directory(state / FOLDER) as fd:
            names = os.listdir(fd)
        for name in names:
            cleanup(state / FOLDER / name, whole=True)
    except OSError:
        pass
