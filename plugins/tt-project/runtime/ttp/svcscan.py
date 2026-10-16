# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Find service unit changes in a script before a worker runs it.

A driver or test stub that calls `systemctl stop|disable|mask|...` (often as `sudo -n systemctl`)
changes a shared machine's services for everyone on it: a serving process stops, a recovery timer
is switched off, a unit stays masked after the run. `ttp svcscan <script>` flags those calls;
`ttp svcscan --shim <dir>` writes stand-ins for `systemctl`, `service` and `sudo` that only log
changing calls, so the driver can be exercised without touching a real unit. Read-only queries
(`systemctl is-active`, `status`, `show`, ...) go on to the real `systemctl` when there is one.
A call by absolute path (`/usr/bin/systemctl`, `/usr/bin/sudo`) or over ssh bypasses a local shim,
so it is flagged as such.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# the systemctl verbs that change a unit's state or enablement
VERBS = ("stop", "disable", "mask", "start", "restart", "enable", "unmask", "try-restart",
         "reload-or-restart", "try-reload-or-restart", "kill", "isolate")
_CMD = r"(?<![\w./-])(?:/\S*/)?"
_END = r"(?![\w.-])"
# options and other words before the verb, staying within one command (no ; | & or newline)
_GAP = r"(?:\s+[^\s;|&]+)*?\s+"
_VERB = r"(?:" + "|".join(re.escape(v) for v in sorted(VERBS, key=len, reverse=True)) + r")" + _END
_RULES = [
    ("systemctl changes a unit on the machine", re.compile(_CMD + r"systemctl" + _END + _GAP + _VERB)),
    ("service changes a unit on the machine",
     re.compile(_CMD + r"service" + r"\s+[\w@.:-]+\s+(?:stop|start|restart|force-reload|try-restart)" + _END)),
]
_ABSOLUTE = re.compile(r"/\S*/(?:systemctl|service|sudo)\b")
_SSH = re.compile(r"(?<![\w./-])(?:/\S*/)?ssh" + _END)
SHIMMED = ("systemctl", "service", "sudo")


def scan(text: str) -> list:
    """[(line number, reason, line)] for each line that changes a service unit. Comment lines are
    skipped; one reason per line. A call by absolute path or over ssh is marked, since a local
    PATH shim does not stop it."""
    found = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.lstrip()
        if line.startswith("#"):
            continue
        for reason, rule in _RULES:
            m = rule.search(line)
            if not m:
                continue
            start = max(line.rfind(c, 0, m.start()) for c in ";|&\n") + 1
            prefix = line[start:m.end()]
            if _ABSOLUTE.search(prefix):
                reason += " (by absolute path: a PATH shim does not stop it)"
            elif _SSH.search(line[:m.start()]):
                reason += " (over ssh: a local PATH shim does not stop it)"
            found.append((n, reason, line.strip()))
            break
    return found


def _systemctl_shim(log: str) -> str:
    verbs = "|".join(VERBS)
    # any changing verb among the words (not only the first one: `-H <host> stop`) is only logged
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs every call; changes no unit, read-only queries go to the real one\n"
            f"printf '%s\\n' \"systemctl $*\" >> {log}\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            f"    {verbs}) exit 0 ;;\n"
            "  esac\n"
            "done\n"
            "self=$(cd \"$(dirname \"$0\")\" && pwd)\n"
            "IFS=:\n"
            "for d in $PATH; do\n"
            "  [ \"$d\" = \"$self\" ] && continue\n"
            "  [ -x \"$d/systemctl\" ] && exec \"$d/systemctl\" \"$@\"\n"
            "done\n"
            "exit 0\n")


def _service_shim(log: str) -> str:
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs the call, changes no unit\n"
            f"printf '%s\\n' \"service $*\" >> {log}\n"
            "exit 0\n")


def _sudo_shim(log: str) -> str:
    # Skip sudo's options (those that take a value too) and VAR=value words; run the command only
    # when it is shimmed here (by basename), so `sudo -n /usr/bin/systemctl stop x` is only logged.
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs the call; runs systemctl/service through their shims, nothing else\n"
            f"printf '%s\\n' \"sudo $*\" >> {log}\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --) shift; break ;;\n"
            "    -u|-g|-p|-C|-D|-r|-t|-U|-T|--user|--group|--prompt|--close-from|--chdir|--host|--role|"
            "--type|--other-user|--command-timeout) shift; [ $# -gt 0 ] && shift ;;\n"
            "    -*) shift ;;\n"
            "    *=*) shift ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            "[ $# -gt 0 ] || exit 0\n"
            "self=$(cd \"$(dirname \"$0\")\" && pwd)\n"
            "name=$(basename \"$1\")\n"
            "case \"$name\" in\n"
            "  systemctl|service) shift; exec \"$self/$name\" \"$@\" ;;\n"
            "esac\n"
            "exit 0\n")


def write_shims(folder: Path) -> list:
    """Write logging stand-ins for systemctl, service and sudo into `folder`. Calls are appended to
    `folder`/svcscan.log; a changing call exits 0 and changes nothing."""
    folder.mkdir(parents=True, exist_ok=True)
    log = shlex.quote(str(folder / "svcscan.log"))
    paths = []
    for name, body in (("systemctl", _systemctl_shim(log)), ("service", _service_shim(log)),
                       ("sudo", _sudo_shim(log))):
        path = folder / name
        path.write_text(body)
        path.chmod(0o755)
        paths.append(path)
    return paths
