"""The first process of a `ttp sandbox` PID namespace (sandbox.py): runs the command as its child
and exits with its exit code, which ends every process left in the namespace. Standalone (run with
`python3 -I -S`): it imports nothing from the runtime.

The command runs in a session and process group of its own, so a `kill 0` or os.killpg(0, sig) in
it reaches only the namespace, never the caller's group outside it. A SIGINT, SIGTERM or SIGHUP to
this process (a terminal's Ctrl-C, a TERM to the caller's group from a timeout) is passed on to the
command's group; a SIGKILL ends the namespace. `unshare --fork` hands its child SIGINT and SIGTERM
ignored; the command gets them back at their defaults, with SIGPIPE, which Python ignores."""
import os
import signal
import sys

RESET = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE)


def main(argv: list) -> int:
    if not argv:
        print("ttp sandbox: no command", file=sys.stderr)
        return 2
    forward = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    # Held until the command's group exists, so a stop that comes first is passed on, not lost.
    signal.pthread_sigmask(signal.SIG_BLOCK, forward)
    try:
        pid = os.posix_spawnp(argv[0], argv, os.environ, setsid=True, setsigmask=(), setsigdef=RESET)
    except OSError as e:
        print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126

    def pass_on(sig, _frame):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass

    for sig in forward:
        signal.signal(sig, pass_on)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, forward)
    while True:
        try:
            done, status = os.wait()
        except ChildProcessError:
            return 1
        if done == pid:   # orphans the namespace adopted are reaped on the way
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
