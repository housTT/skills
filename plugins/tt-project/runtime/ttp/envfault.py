# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check runner the host lacks: an environment fault, not a failed check.

Before check commands run (`ttp checks`, `ttp push`, the push queue's batch), precheck() looks at
each one's runners: the program of its first simple command must be on PATH (or an existing absolute
path), and a `python -m <module>` there (a bare pytest included, after push.venv_cmd) must find
that module with that python. One that cannot is a fault with the signature env:missing-runner:<name>.
It counts against no change or branch: `ttp checks` and `ttp push` exit 75 (wait) and print a
retry_when (`ttp checks --env-ok`), and the push queue requeues the batch and holds until the
runner is back. Each fault is recorded once in the project's state, with one coordinator
observation per fault however many tasks hit it; it clears once its probe passes."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

KV = "env_faults"
PREFIX = "env:missing-runner:"
PROBE_S = 60            # the push queue's recheck of open faults: at most once per this
PROBE_TIMEOUT_S = 30
_PY = re.compile(r"python[0-9.]*$")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_OPS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&", "\n"}
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "time"}
_SKIP = {"for", "case", "function", "select", "[[", "[", "test"}   # the segment is no plain program run
_WRAPPERS = {"nice", "nohup"}
_FIND = ("import importlib.util, sys\n"
         "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 3)")


class Fault(Exception):
    """Raised by a check runner that found a fault (the push queue's batch); `.fault` is its dict."""

    def __init__(self, fault: dict):
        super().__init__(line(fault))
        self.fault = fault


def _segments(cmd: str) -> list[list[str]]:
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segs, cur = [], []
    for tok in lex:
        if tok in _OPS or set(tok) <= set(";&|()"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _program(words: list[str]) -> tuple[str, list[str]] | None:
    """(program, its arguments) of one simple command, past assignments, keywords and wrappers
    (`env`, `timeout <duration>`, `nice`, ...); None when it is no plain program run."""
    i = 0
    while i < len(words):
        w = words[i]
        if _ASSIGN.match(w) or w in _KEYWORDS:
            i += 1
        elif w == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGN.match(words[i])):
                i += 1
        elif w == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1   # the duration
        elif w in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] == "-n" else 1
        else:
            break
    if i >= len(words) or words[i] in _SKIP or any(c in words[i] for c in "$`*?"):
        return None
    return words[i], words[i + 1:]


def _module(args: list[str]) -> str | None:
    """The module of a `python [options] -m <module>` run, else None."""
    for j, w in enumerate(args):
        if w == "-m" and j + 1 < len(args):
            return args[j + 1]
        if w.startswith("-m") and len(w) > 2:
            return w[2:]
        if not w.startswith("-") or w in ("-c", "-"):
            return None
    return None


def _resolve(prog: str, path: str) -> str | None:
    if "/" in prog:
        return prog if os.access(prog, os.X_OK) else None
    return shutil.which(prog, path=path)


def runners(cmd: str, top: str | Path, env: dict | None = None) -> list[dict]:
    """The runner `cmd` needs first as it would run in `top` (push.venv_cmd applied): a probe of the
    program of its first simple command that runs one, or of its `python -m <module>` when that
    module is not the repository's own. Only the first: a later command may run what an earlier one
    installs. None after a step that may change PATH (source, export, eval, a PATH= assignment)."""
    from . import push
    env = dict(os.environ if env is None else env)
    path = env.get("PATH") or os.defpath
    try:
        segs = _segments(push.venv_cmd(cmd, top, env))
    except ValueError:
        return []   # does not parse: the run says what is wrong
    for words in segs:
        if any(w.startswith("PATH=") for w in words[:1]) or words[:1] in (["source"], ["."], ["export"],
                                                                          ["eval"], ["exec"]):
            return []
        if words[0] in _SKIP or words[0] in _KEYWORDS - {"time", "!"}:
            return []   # a conditional or loop: what runs depends on it
        got = _program(words)
        if not got:
            continue
        prog, args = got
        if any(w.startswith("PATH=") for w in words if _ASSIGN.match(w)):
            return []
        if "/" not in prog and prog in push.BUILTINS:
            continue
        if "/" in prog and not os.path.isabs(prog):
            return []   # a script of the repository's: the change's business
        mod = _module(args) if _PY.search(Path(prog).name) else None
        if mod and not push._in_repo(mod.split(".")[0], top):
            return [{"kind": "module", "python": prog, "module": mod.split(".")[0], "path": path,
                     "cmd": cmd, "cwd": str(top)}]
        return [{"kind": "program", "name": prog, "path": path, "cmd": cmd, "cwd": str(top)}]
    return []


def probe(r: dict, env: dict | None = None) -> dict | None:
    """The fault of one runner (runners()), or None when it is there. A probe that cannot tell (a
    timeout, an interpreter that crashes) is no fault: the check runs and says what it says."""
    path = str(r.get("path") or os.defpath)
    if r.get("kind") == "program":
        name = str(r.get("name") or "")
        if _resolve(name, path):
            return None
        return {**r, "sig": PREFIX + Path(name).name, "runner": Path(name).name, "python": ""}
    py = _resolve(str(r.get("python") or ""), path)
    if not py:
        name = Path(str(r.get("python") or "python")).name
        return {**r, "sig": PREFIX + name, "runner": name, "python": str(r.get("python") or "")}
    run_env = {**(os.environ if env is None else env), "PATH": path}
    cwd = r.get("cwd") if r.get("cwd") and Path(str(r["cwd"])).is_dir() else None
    try:
        rc = subprocess.run([py, "-c", _FIND, str(r["module"])], cwd=cwd, env=run_env, capture_output=True,
                            timeout=PROBE_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError):
        return None
    if rc != 3:
        return None
    return {**r, "sig": PREFIX + str(r["module"]), "runner": str(r["module"]), "python": py}


def precheck(cmds: list, top: str | Path, env: dict | None = None) -> dict | None:
    """The first runner fault of the check commands `cmds` run in `top` with `env`, or None."""
    for c in cmds:
        for r in runners(str(c), top, env):
            if f := probe(r, env):
                return f
    return None


def line(f: dict) -> str:
    what = (f"{f['python']} cannot import {f['runner']}" if f.get("kind") == "module" and f.get("python")
            else f"{f.get('runner')} is not on PATH or not executable")
    return (f"environment fault {f.get('sig')}: {what} (needed by {f.get('cmd')!r}). This is the host, "
            "not the change: nothing is counted against it")


def record(p, f: dict, task: str | None = None) -> bool:
    """Keep the fault open in the project's state, and raise one coordinator observation the first
    time its signature is seen in this episode. True when this call raised it. Never raises."""
    if p is None:
        return False
    try:
        db = p.db
        now = time.time()
        with db.tx():
            faults = db.kv(KV, {}) or {}
            sig = str(f["sig"])
            old = faults.get(sig) if isinstance(faults.get(sig), dict) else None
            tasks = list((old or {}).get("tasks") or [])
            if task and task not in tasks:
                tasks = (tasks + [task])[-20:]
            keep = {k: f.get(k) for k in ("kind", "name", "python", "module", "path", "cwd", "cmd", "runner")
                    if f.get(k) is not None}
            faults[sig] = {**keep, "since": (old or {}).get("since") or now, "last": now, "tasks": tasks,
                           "checked": now}
            db.set_kv(KV, faults)
            if old:
                return False
            fp = f"{sig}:{now:.0f}"
            py = f" with {f['python']}" if f.get("python") else ""
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "checks", "observation", fp, "high",
                  f"Environment fault {sig}: the check runner {f.get('runner')} cannot be found or imported"
                  f"{py} (first seen in {f.get('cmd')!r}). Check runs wait instead of failing and nothing "
                  "is counted against any change; the push queue holds its batch. Restore the runner on "
                  "the host (reinstall it for that python, or point the checks at a venv). Waiting tasks "
                  "and the queue resume once `ttp checks --env-ok` passes.", "queued"))
        return True
    except Exception:   # a note about the host never stops the caller from waiting
        return False


def open_faults(p) -> dict:
    try:
        v = p.db.kv(KV, {}) or {}
    except Exception:
        return {}
    return {k: f for k, f in v.items() if isinstance(f, dict)} if isinstance(v, dict) else {}


def recheck(p, now: float | None = None, every_s: float = 0) -> dict:
    """Probe each open fault again (skipping those probed within `every_s`), close the ones that
    pass and return those still open."""
    now = time.time() if now is None else now
    faults = open_faults(p)
    if not faults:
        return {}
    still, changed = {}, False
    for sig, f in faults.items():
        if every_s and now - float(f.get("checked") or 0) < every_s:
            still[sig] = f
            continue
        changed = True
        if probe(f):
            still[sig] = {**f, "checked": now}
    if changed:
        try:
            with p.db.tx():
                cur = p.db.kv(KV, {}) or {}
                # A fault another caller recorded meanwhile stays.
                cur = {k: v for k, v in cur.items() if k in still or k not in faults}
                cur.update(still)
                p.db.set_kv(KV, cur)
        except Exception:
            pass
    return still


def held(p, now: float | None = None) -> str:
    """Why the push queue holds (an open fault whose runner is still missing), or ""."""
    if not open_faults(p):
        return ""
    still = recheck(p, now, PROBE_S)
    if not still:
        return ""
    return "held: environment fault " + ", ".join(sorted(still)) + " (a check runner is missing on this host)"


def retry_when(p) -> str:
    from . import push
    return f"{push._own_ttp(p)} checks --env-ok" if p else "ttp checks --env-ok"


def write_marker(run_dir: str | Path | None, f: dict) -> None:
    if not run_dir:
        return
    from .project import write_json
    try:
        write_json(Path(run_dir) / MARKER, {**f, "ts": time.time()})
    except OSError:
        pass


MARKER = "env_fault.json"   # in a run's directory: its checks stopped at a runner fault


def marker_open(run_dir: Path) -> dict | None:
    """The fault a run's checks stopped at, while its runner is still missing; else None."""
    import json
    try:
        f: Any = json.loads((Path(run_dir) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(f, dict):
        return None
    return probe(f)
