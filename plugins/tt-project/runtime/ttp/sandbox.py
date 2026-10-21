"""A process sandbox for check and test commands: `ttp sandbox -- <cmd...>`, and every check the
runtime runs (push.check_argv).

The command runs in a PID namespace of its own with a private /proc, so it sees only its own process
tree: a kill, pkill or os.kill aimed at any other pid on the host reaches nothing, and every process
it started ends with it. Network, files, environment, working directory and exit code stay as they
are. It needs unprivileged user namespaces and util-linux `unshare` or bubblewrap `bwrap`; where
neither works (a sysctl or AppArmor setting, a missing tool, not Linux) commands run as before and
`notice` says why, once per run. A command already in a sandbox runs as it is."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

INIT = Path(__file__).with_name("sandbox_init.py")
PROBE_S = 20
# The sysctls that turn unprivileged user namespaces off: (name, value that does, what turns them on).
SETTINGS = (("kernel.apparmor_restrict_unprivileged_userns", "1",
             "set it to 0, or give unshare or bwrap an AppArmor profile that allows user namespaces"),
            ("kernel.unprivileged_userns_clone", "0", "set it to 1"),
            ("user.max_user_namespaces", "0", "set it above 0"))
SYSCTL_DIR = Path("/proc/sys")
_STATE: dict[str, tuple[list[str] | None, str]] = {}   # by PATH: what status() found


def _tools() -> list[tuple[str, list[str]]]:
    out = []
    if u := shutil.which("unshare"):
        out.append(("unshare", [u, "--user", "--map-current-user", "--pid", "--fork", "--mount-proc", "--kill-child"]))
    if b := shutil.which("bwrap"):
        out.append(("bwrap", [b, "--unshare-pid", "--die-with-parent", "--dev-bind", "/", "/", "--proc", "/proc"]))
    return out


def _init() -> list[str]:
    return [sys.executable or "python3", "-I", "-S", str(INIT)]


def _setting() -> str:
    """The kernel setting that keeps user namespaces off and how to turn them on, or ""."""
    for name, off, fix in SETTINGS:
        try:
            if (SYSCTL_DIR / name.replace(".", "/")).read_text().strip() == off:
                return f"sysctl {name}={off} turns off unprivileged user namespaces ({fix})"
        except OSError:
            pass
    return ""


def _probe() -> tuple[list[str] | None, str]:
    if not sys.platform.startswith("linux"):
        return None, "PID namespaces need Linux"
    tools = _tools()
    if not tools:
        return None, "neither unshare (util-linux) nor bwrap (bubblewrap) is installed; install either"
    # Proof of isolation, not only a start: this process must be out of sight inside.
    me = os.getpid()
    test = ["sh", "-c", f"test ! -e /proc/{me}"] if me > 10 else ["true"]
    errors = []
    for name, pre in tools:
        try:
            r = subprocess.run([*pre, *_init(), *test], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=PROBE_S)
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{name}: {e}")
            continue
        if r.returncode == 0:
            return pre, ""
        last = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        errors.append(f"{name}: {last}")
    why = "; ".join(errors)
    return None, (f"{setting} ({why})" if (setting := _setting()) else why)


def status() -> tuple[list[str] | None, str]:
    """(the argv prefix that sandboxes a command, "") where a sandbox works, else (None, why).
    Probed once per process and PATH."""
    key = os.environ.get("PATH", "")
    if key not in _STATE:
        _STATE[key] = _probe()
    return _STATE[key]


def inside() -> bool:
    """This process already runs in a `ttp sandbox` (its init is the namespace's first or, under
    bwrap, second process)."""
    for pid in (1, 2):
        try:
            if INIT.name.encode() in Path(f"/proc/{pid}/cmdline").read_bytes():
                return True
        except OSError:
            pass
    return False


def wrap(argv: list[str], on: bool = True) -> list[str]:
    """`argv` run in a sandbox where one works and `on`; as it is otherwise or when already in one."""
    if not on or inside():
        return list(argv)
    pre, _ = status()
    return [*pre, *_init(), *argv] if pre else list(argv)


def notice(on: bool = True) -> str:
    """One line for a run's output when its commands run without the sandbox; "" when they run in one."""
    if not on:
        return ("sandbox off (\"sandbox\": false): this check sees and can signal every process of "
                "this user on the host")
    if inside():
        return ""
    pre, why = status()
    return "" if pre else f"no process sandbox: {why}. Commands run unisolated and can see and signal other processes"


def run(argv: list[str]) -> int:
    """`ttp sandbox -- <cmd...>`: run `argv` sandboxed and return its exit code. A SIGTERM or SIGHUP
    to this process (a `timeout`, a closed terminal) ends the whole namespace."""
    wrapped = wrap(argv)
    if wrapped == list(argv):
        if line := notice():
            print(f"ttp sandbox: {line}", file=sys.stderr, flush=True)
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
            return 127 if isinstance(e, FileNotFoundError) else 126
    proc = subprocess.Popen(wrapped)

    def end(*_):
        try:
            proc.kill()   # unshare --kill-child / bwrap --die-with-parent: the namespace goes with it
        except OSError:
            pass

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGHUP, end)
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # a terminal's Ctrl-C reaches the command itself
    rc = proc.wait()
    return rc if rc >= 0 else 128 - rc
