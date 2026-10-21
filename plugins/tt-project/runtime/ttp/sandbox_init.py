"""The first process of a `ttp sandbox` PID namespace (sandbox.py): runs the command as its child
and exits with its exit code, which ends every process left in the namespace. Standalone (run with
`python3 -I -S`): it imports nothing from the runtime.

As the namespace's first process it ignores signals it has no handler for, so a stop reaches the
command through the process group (the command shares the caller's) or as SIGKILL, which ends the
namespace. `unshare --fork` hands its child SIGINT and SIGTERM ignored; the command gets them back
at their defaults, with SIGPIPE, which Python ignores."""
import os
import signal
import sys

RESET = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE)


def main(argv: list) -> int:
    if not argv:
        print("ttp sandbox: no command", file=sys.stderr)
        return 2
    pid = os.fork()
    if pid == 0:
        for sig in RESET:
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
            os._exit(127 if isinstance(e, FileNotFoundError) else 126)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
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
