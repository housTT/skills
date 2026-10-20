# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check that runs on another machine over ssh: `delivery.checks_host`, or a check's own `host`.

Some projects may not run test suites on the machine that coordinates them. A check with a host
runs there instead: the exact commit being checked (HEAD of the checkout the check runs in) is
copied with `git archive` over ssh into a fresh temporary directory, made a git checkout (`git init`
and one commit of that tree, so tests that need a repository work), and the command runs in it with
the check's `env` exported (values expand in the remote shell, e.g. "$HOME/.local/bin:$PATH"). Its
output streams back and its exit code is the check's. The temporary directory is always removed.

ssh failing (an unreachable host, a refused key, a dropped connection) or a host that cannot take
the tree is an environment fault, not the change's: this exits FAULT_RC, which the callers
(`ttp checks`, `ttp push`, the push queue) treat as envfault does a missing runner. A command's own
exit FAULT_RC is reported as 1, so it never reads as a fault.

`host` is an ssh alias (a machines-list alias is one): `ssh <host>` must reach it without a prompt."""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from pathlib import Path

FAULT_RC = 75
LOCAL = "local"      # a check's `host` that keeps it here when delivery.checks_host is set
HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+:-]{0,252}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
SETUP_TIMEOUT_S = 900
CLEANUP_TIMEOUT_S = 120
PROBE_TIMEOUT_S = 45
SIG = "env:check-host:"
_TMP = re.compile(r"/[A-Za-z0-9._/-]*/ttp-check\.[A-Za-z0-9]+")
# Runs the module from this runtime whatever the check's PYTHONPATH: argv[1] is the runtime folder.
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from ttp.remotecheck import main; sys.exit(main(sys.argv[2:]))"


def host_problem(v) -> str:
    """Why `v` cannot be a check host, or ""."""
    if not isinstance(v, str) or not HOST_RE.fullmatch(v.strip()):
        return f"{v!r}: a check host is an ssh alias (letters, digits and . _ - @ + :), or {LOCAL!r}"
    return ""


def env_problem(v) -> str:
    """Why `v` cannot be a check's remote `env`, or ""."""
    if not isinstance(v, dict) or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(x, str)
                                          for k, x in v.items()):
        return f"{v!r}: env must map variable names to string values"
    return ""


def argv(cmd: str, host: str, env: dict | None = None) -> list[str]:
    """The local command that runs check `cmd` on `host` (main)."""
    return [sys.executable, "-c", _BOOT, str(Path(__file__).resolve().parents[1]), "--host", host,
            "--env", json.dumps(env or {}, sort_keys=True), "--", cmd]


def _ssh(host: str, script: str) -> list[str]:
    # The login shell parses the command line: hand it one quoted word for sh, whatever that shell is.
    return [*SSH, host, f"sh -c {shlex.quote(script)}"]


def reachable(host: str) -> str:
    """"" when ssh reaches `host` and it has git and tar (what a remote check needs), else why not."""
    try:
        r = subprocess.run(_ssh(host, "command -v git >/dev/null && command -v tar >/dev/null"),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"ssh to {host} timed out after {PROBE_TIMEOUT_S} s"
    except OSError as e:
        return f"cannot run ssh: {e}"
    if r.returncode == 0:
        return ""
    if r.returncode == 255:
        return f"ssh cannot reach {host}: {(r.stderr.strip().splitlines() or ['exit 255'])[-1][:200]}"
    return f"{host} lacks git or tar"


def fault(host: str, cmd: str, cwd: str, why: str = "") -> dict:
    """The envfault record of a check host that cannot run checks."""
    return {"kind": "host", "host": host, "runner": host, "sig": SIG + host, "cmd": cmd, "cwd": cwd,
            **({"why": why} if why else {})}


def _say(msg: str) -> None:
    sys.stdout.write(f"ttp remote check: {msg}\n")
    sys.stdout.flush()


def _fault(host: str, why: str) -> int:
    _say(f"environment fault {SIG}{host}: {why}. This is the host, not the change")
    return FAULT_RC


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def exports(env: dict) -> str:
    # Double quotes: a value may expand remote variables ($HOME, $PATH); \ " and ` stay literal.
    return "".join(f'export {k}="' + re.sub(r'([\\"`])', r"\\\1", v) + '"\n' for k, v in sorted(env.items()))


class _Stop(Exception):
    pass


def _stop(_sig, _frame) -> None:
    raise _Stop()


def main(args: list[str]) -> int:
    host, env = "", {}
    while args and args[0] != "--":
        flag, value, args = args[0], args[1] if len(args) > 1 else "", args[2:]
        if flag == "--host":
            host = value
        elif flag == "--env":
            env = json.loads(value or "{}")
    cmd = " ".join(args[1:])
    if host_problem(host) or host == LOCAL or env_problem(env) or not cmd:
        _say(f"usage: --host <ssh alias> [--env <json>] -- <command> ({host_problem(host) or env_problem(env)})")
        return 2
    top, head = _git("rev-parse", "--show-toplevel"), _git("rev-parse", "HEAD")
    if not top or not head:
        _say("not in a git checkout with a commit; nothing to copy")
        return 2
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _stop)
    tmp, proc = "", None
    try:
        try:
            made = subprocess.run(_ssh(host, 'mktemp -d "${TMPDIR:-/tmp}/ttp-check.XXXXXXXX"'),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _fault(host, f"ssh timed out after {PROBE_TIMEOUT_S} s")
        tmp = made.stdout.strip()
        if made.returncode != 0 or not _TMP.fullmatch(tmp):
            tmp = ""
            return _fault(host, f"cannot make a temporary directory there (ssh exit {made.returncode}: "
                                f"{(made.stderr.strip().splitlines() or [''])[-1][:200]})")
        _say(f"checking {head[:12]} on {host} in {tmp}")
        q = shlex.quote(tmp)
        setup = (f"set -e; cd {q}; tar -xf -; git -c init.defaultBranch=main init -q; git add -A -f .; "
                 f"git -c user.name=ttp -c user.email=ttp@localhost -c commit.gpgsign=false commit -q "
                 f"--no-verify --allow-empty -m {shlex.quote('ttp check of ' + head)}")
        archive = subprocess.Popen(["git", "-C", top, "archive", "--format=tar", head], stdout=subprocess.PIPE)
        try:
            copied = subprocess.run(_ssh(host, setup), stdin=archive.stdout, capture_output=True, text=True,
                                    timeout=SETUP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            archive.kill()
            return _fault(host, f"copying the tree timed out after {SETUP_TIMEOUT_S} s")
        finally:
            archive.stdout.close()
            archive.wait()
        if archive.returncode != 0:
            _say(f"git archive of {head[:12]} failed (exit {archive.returncode})")
            return 2
        if copied.returncode != 0:
            return _fault(host, f"cannot unpack the tree into a git checkout there (exit {copied.returncode}: "
                                f"{(copied.stderr.strip().splitlines() or [''])[-1][:200]})")
        mark = f"ttp-remote-rc:{secrets.token_hex(8)}:"
        script = (f"cd {q} || exit 97\ntrap 'cd / && rm -rf {q}' EXIT\n{exports(env)}"
                  f"if command -v bash >/dev/null 2>&1; then bash -e -o pipefail -c {shlex.quote(cmd)}; "
                  f"else sh -e -c {shlex.quote(cmd)}; fi\nrc=$?\nprintf '\\n%s%s\\n' {mark} \"$rc\"\nexit $rc\n")
        proc = subprocess.Popen(_ssh(host, script), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        rc, held = None, b""
        out = sys.stdout.buffer
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode(errors="replace").strip()
            if text.startswith(mark) and text[len(mark):].isdigit():
                rc = int(text[len(mark):])
                held = b""   # the blank line printed before the marker is ours
                continue
            if held:
                out.write(held)
            held = raw if raw in (b"\n", b"\r\n") else b""
            if not held:
                out.write(raw)
            out.flush()
        proc.wait()
        if rc is None:
            return _fault(host, f"ssh ended before the command reported its exit code (ssh exit {proc.returncode})")
        if rc == FAULT_RC:
            _say(f"the command exited {FAULT_RC}; reported as 1, so it does not read as an unreachable host")
            return 1
        return rc
    except _Stop:
        return 143
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if tmp:
            try:
                subprocess.run(_ssh(host, f"rm -rf {shlex.quote(tmp)}"), stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=CLEANUP_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError):
                _say(f"could not remove {tmp} on {host}")
