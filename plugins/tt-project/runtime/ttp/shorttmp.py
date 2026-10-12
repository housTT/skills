# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Short temp dirs for checks, detached jobs and fenced workers: /tmp/ttp-<uid>/<8 hex>.

A temp dir under the project's state is long, and tests add more to it (pytest's
pytest-of-<user>/pytest-N/<test>0): an AF_UNIX socket there passes the 108-byte limit (104 on
macOS) and fails with "AF_UNIX path too long". So each allocation gets a dir of its own under a
per-user 0700 root in the system temp root, which its owner records (owner.json, a detach job's
record, run.json) so finishing and recovery remove it. The root is named without links in its
base (/private/tmp on macOS) and is refused when it is a link, not a
directory or not this user's; callers then keep their old path under the state. A fenced worker
may write the whole root, as it may write the whole project state: it starts checks and detached
jobs itself.
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path

BASE = '/tmp'   # short on every supported host; tempfile.gettempdir() is long on macOS
_NAME = re.compile(r'[0-9a-f]{8}')


def root() -> Path:
    # The base without links (macOS /tmp is a link to /private/tmp): checks validate a recorded dir
    # component by component without following links. ttp-<uid> itself is still refused as a link.
    # Tests point it at a short folder of their own.
    base = os.path.realpath(os.environ.get('TTP_TEST_SHORT_TMP') or BASE)
    return Path(base) / f'ttp-{os.getuid()}'


def _open_root(create: bool) -> int:
    """The root, pinned, only when it is this user's own directory (made 0700 if it is not)."""
    if create:
        try:
            os.mkdir(root(), 0o700)
        except FileExistsError:
            pass
    fd = os.open(root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f'{root()} is not this user\'s own directory')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def ready() -> bool:
    """The root exists and is usable (a fence grants it to a worker, which makes dirs in it)."""
    try:
        os.close(_open_root(create=True))
        return True
    except OSError:
        return False


def make() -> Path | None:
    """A new 0700 dir under the root, or None when the root is unusable (callers fall back)."""
    try:
        fd = _open_root(create=True)
    except OSError:
        return None
    try:
        for _ in range(8):
            name = uuid.uuid4().hex[:8]
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                return root() / name
            except FileExistsError:
                continue
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def owned(value) -> bool:
    """A dir make() could have returned: a direct child of the root with its name pattern."""
    return (isinstance(value, (str, Path)) and bool(str(value)) and Path(value).parent == root()
            and bool(_NAME.fullmatch(Path(value).name)))


def remove(value) -> None:
    """Remove a dir make() returned, only through pinned descriptors; anything else is left alone."""
    if not owned(value):
        return
    from .checkdirs import _remove
    try:
        fd = _open_root(create=False)
    except OSError:
        return
    try:
        _remove(fd, Path(value).name)
    except OSError:
        pass
    finally:
        os.close(fd)
