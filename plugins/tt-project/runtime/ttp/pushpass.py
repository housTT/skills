# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Push-check passes the push queue recorded itself, so a batch retried after being cut short (a
reboot) does not run again a check that already passed on the same merged tree.

A pass is keyed on the tree of the checked head, the exact check command and env_id: the
interpreters and venv the check would use and the tip it was checked against. Only passes are
recorded: a failure, a check skipped by if_exists or if_changed and one never run leave nothing. The
batch reads only what a batch recorded here, never the passes `ttp checks` records; it trusts the
project's state as it trusts its own markers there, which name the heads it pushes. The store keeps
the newest MAX entries younger than MAX_AGE_S.

A check that is one plain pytest run also resumes (resumable): the ttp_pytest_resume plugin appends
each test file whose every selected test passed to a file keyed the same way, and the next run of
that check under the same key deselects those files and runs the rest. Files are skipped whole,
never single tests. A whole pass removes the file; the newest RESUME_MAX younger than
RESUME_MAX_AGE_S are kept."""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import push
from .project import Project, write_json

STORE = "push_check_passes.json"     # under the project's state: {"passes": [{"key", "tree", "cmd", "batch", "ts"}]}
MAX, MAX_AGE_S = 200, 14 * 86400
RESUME_DIR = "push_check_resume"     # under the project's state: <key>.txt, one passed test file per line
RESUME_MAX, RESUME_MAX_AGE_S = 20, 3 * 86400
PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_resume"
PLUGIN = "ttp_pytest_resume"
# A check that sets these itself would drop the plugin or the path it loads from.
_OWN_ENV = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "env -i")


def tree_of(wt: Path, head: str) -> str:
    r = subprocess.run(["git", "-C", str(wt), "rev-parse", f"{head}^{{tree}}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _stat(path: str | Path) -> list:
    try:
        st = os.stat(path)
        return [str(path), st.st_ino, st.st_size, st.st_mtime_ns]
    except OSError:
        return [str(path), None]


def _python(py: str | Path | None) -> list:
    """What tells one interpreter or venv from another: the binary it resolves to, and for a venv its
    config and package folders (installing or removing a package changes their times)."""
    if not py:
        return []
    real = os.path.realpath(py)
    venv = Path(os.path.realpath(Path(py).parent.parent))
    out = [_stat(real)]
    if (venv / "pyvenv.cfg").exists():
        out += [_stat(venv / "pyvenv.cfg")] + [_stat(d) for d in sorted(glob.glob(str(venv / "lib*" / "python*" / "site-packages")))]
    return out


def env_id(wt: Path, cmd: str, env: dict, tip: str) -> str:
    """The environment part of a pass's key: the command as it runs (with the venv python it is
    given), that venv's identity, the python3 on the check's PATH, the active venv and the tip."""
    argv = push.check_argv(cmd, wt, env)
    which = shutil.which("python3", path=env.get("PATH"))
    parts = {"argv": argv, "venv": _python(push.venv_python(wt, env)), "python3": _python(which),
             "active": env.get("VIRTUAL_ENV") or "", "tip": tip or ""}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# Beyond env_id, what changes the tests a check runs or how: so a `ttp checks` pass serves
# `ttp push --own` only where these match too.
CHECK_ENV_VARS = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD")


def check_envs(wt: Path, cmds: list, env: dict) -> list[str]:
    """Per command, the environment a `ttp checks` pass records and `ttp push --own` compares
    before it reuses that pass: env_id without a tip (`ttp checks` checks against none), plus
    CHECK_ENV_VARS as `env` sets them."""
    own = {k: env.get(k, "") for k in CHECK_ENV_VARS}
    return [hashlib.sha256(json.dumps([env_id(wt, str(c), env, ""), own]).encode()).hexdigest() for c in cmds]


def key(tree: str, cmd: str, eid: str) -> str:
    return hashlib.sha256(json.dumps([tree, cmd, eid]).encode()).hexdigest()


def _load(path: Path) -> list[dict]:
    try:
        passes = json.loads(path.read_text()).get("passes")
    except (OSError, ValueError, AttributeError):
        return []
    return [e for e in passes if isinstance(e, dict)] if isinstance(passes, list) else []


def _fresh(entries: list[dict], now: float) -> list[dict]:
    return [e for e in entries if now - float(e.get("ts") or 0) < MAX_AGE_S][-MAX:]


def passed(p: Project, tree: str, cmd: str, eid: str) -> dict | None:
    """The recorded pass of `cmd` on `tree` under `eid`, or None."""
    if not tree:
        return None
    k, now = key(tree, cmd, eid), time.time()
    for e in reversed(_fresh(_load(p.state / STORE), now)):
        if e.get("key") == k:
            return e
    return None


def record(p: Project, tree: str, cmd: str, eid: str, batch: str) -> str:
    """Record a pass (only ever a pass); prune the store. Returns why it could not, or ""."""
    if not tree:
        return "no tree"
    path = p.state / STORE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            now = time.time()
            entries = _load(path) + [{"key": key(tree, cmd, eid), "tree": tree, "cmd": cmd, "batch": batch, "ts": now}]
            write_json(path, {"passes": _fresh(entries, now)})
    except OSError as e:
        return str(e)
    return ""


def resumable(cmd: str) -> bool:
    """`cmd` runs pytest exactly once and sets none of what loads the resume plugin."""
    return len(push._PYTEST_RUN.findall(cmd)) == 1 and not any(w in cmd for w in _OWN_ENV)


def resume_file(p: Project, tree: str, cmd: str, eid: str) -> Path:
    return p.state / RESUME_DIR / f"{key(tree, cmd, eid)}.txt"


def resumed(path: Path) -> list[str]:
    """The test files recorded passing in `path` (a last line cut mid-write is not one)."""
    try:
        lines = path.read_text().split("\n")
    except OSError:
        return []
    return [line for line in lines[:-1] if line]


def resume_env(path: Path, env: dict) -> dict:
    """The variables that load the resume plugin with `path` as its record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prune(path.parent, keep=path)
    path.touch()
    pp = os.pathsep.join(filter(None, (str(PLUGIN_DIR), env.get("PYTHONPATH", ""))))
    opts = " ".join(filter(None, (env.get("PYTEST_ADDOPTS", ""), f"-p {PLUGIN}")))
    return {"PYTHONPATH": pp, "PYTEST_ADDOPTS": opts, "TTP_PYTEST_RESUME": str(path)}


def prune(folder: Path, keep: Path | None = None) -> None:
    """Drop resume files older than RESUME_MAX_AGE_S and all but the newest RESUME_MAX."""
    now = time.time()
    try:
        files = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    for n, f in enumerate(files):
        try:
            if f != keep and (n >= RESUME_MAX or now - f.stat().st_mtime > RESUME_MAX_AGE_S):
                f.unlink()
        except OSError:
            pass
