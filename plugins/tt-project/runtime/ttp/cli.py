# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp` — create, find, talk to and operate tt-project projects.

Every project command takes the project NAME. If the registry says the project lives on another
machine, the command is forwarded over ssh to that machine's copy of the project's own `ttp`.
"""
from __future__ import annotations

import argparse
import atexit
import getpass
import hashlib
import json
import os
import re
import secrets as pysecrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, poll_s
from .db import chat_floor, task_outcome
from . import outbox
from .devq import unmask_devices
from . import timefmt
from . import schedule as sched
from .project import (FOLDER, NAME_RE, Project, hostname, load_registry, load_secrets, register, save_secret,
                      durable_append, durable_write, write_json, ACCOUNT_KEYS, DEFAULT_CONFIG, deep_merge,
                      load_account_settings, set_account_setting, zombie, device_timeout_max, command_env,
                      NOTE_RUN_ENV, in_run)

RUNTIME = Path(__file__).resolve().parent.parent              # .../runtime (plugin or project copy)
PLUGIN_ROOT = RUNTIME.parent                                   # plugin root, or a project's harness/
MARKER = "tt-project://{name}@{host}:{dir}"


def die(msg: str, code: int = 2) -> None:
    print(f"ttp: {msg}", file=sys.stderr)
    sys.exit(code)


# locating projects --------------------------------------------------------------------------------
def local_project(name: str) -> Project | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") in (None, hostname()) and Path(entry["dir"]).is_dir():
        return Project(entry["dir"])
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        cand = Project(d)
        if cand.exists() and cand.name == name:
            return cand
    return None


def remote_entry(name: str) -> dict | None:
    entry = load_registry().get("projects", {}).get(name)
    if entry and entry.get("host") and entry["host"] != hostname():
        return entry
    return None


def remote_hosts() -> list[str]:
    """The other machines this user's projects run on (projects created with --host)."""
    here = hostname()
    return sorted({e.get("ssh") or e["host"] for e in load_registry().get("projects", {}).values()
                   if isinstance(e, dict) and e.get("host") and e["host"] != here})


LISTEN_GAVE_UP = 75   # `ttp listen --timeout` on another machine: still unreachable when the timeout ran out
LISTEN_REPLACED = 3   # a newer listener for the same chat took over; this one stops and does not reconnect
REPLACED_MARK = "ttp: replaced by a newer listener"   # its stderr line, read even when ssh reports a drop
_MSG_LINE = re.compile(r"\[#(\d+) ")


def _dropped(rc: int) -> bool:
    """True if a forwarded command ended because the connection did, not by its own exit.

    ssh exits 255 when it fails. A status of 128 or more is a signal ending the session (the far
    side's shell, a shared ssh connection, the network stack): `ttp listen` itself exits only
    0, 1, 2 or LISTEN_REPLACED. Below 0, ssh here was killed by a signal."""
    return rc == 255 or rc >= 128 or rc < 0


def _listen_timeout(argv: list[str]) -> float:
    for i, a in enumerate(argv):
        v = a.split("=", 1)[1] if a.startswith("--timeout=") else (argv[i + 1] if a == "--timeout" and i + 1 < len(argv) else None)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                return 0.0
    return 0.0


def _with_timeout(argv: list[str], left: float) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--timeout":
            skip = True
        elif not a.startswith("--timeout="):
            out.append(a)
    return out + [f"--timeout={max(left, 1.0):.0f}"]


def forward_listen(entry: dict, argv: list[str], name: str | None = None) -> int:
    """A remote listener outlives network drops: a laptop changes networks, sleeps and wakes.

    A dropped connection (see _dropped) means this machine lost the path, not that the project
    stopped, so wait (5 s, doubling to 2 min) and reconnect. The listener left on the far side
    exits on its own once its session is gone, and the new one replaces it if it has not yet.
    Each (re)connect first sends what is queued here. A message is printed once per listener:
    one shown before a drop is not shown again after the reconnect, and with --once, a batch that
    arrived before the drop ends the listener (exit 0) like a clean finish. Acknowledging stays
    with --ack, so nothing a reader missed is lost. With --timeout, it stops retrying once the
    timeout has run out and exits LISTEN_GAVE_UP; every other exit is the far side's own.
    A listener replaced by a newer one for the same chat ends with LISTEN_REPLACED and is not
    retried: reconnecting would displace the newer one in turn, and the two would take turns.
    """
    once = "--once" in argv
    limit = _listen_timeout(argv)
    deadline = time.time() + limit if limit > 0 else None
    shown: set[int] = set()
    delay, told, held = 5.0, False, False
    while True:
        failed = flush_outbox(name, entry, quiet=held) if name else None
        held = held or failed is not None
        if failed is not None and failed.returncode == 255:
            if not told:
                _unreachable(entry, failed.stderr)
            rc = 255
        else:
            left = deadline - time.time() if deadline else 0
            rc = forward(entry, _with_timeout(argv, left) if deadline and told else argv, quiet=told, shown=shown)
            if _dropped(rc) and rc != 255 and not told:     # forward() already explained a 255
                _unreachable(entry, None, rc)
        if not _dropped(rc):
            return rc
        if once and shown:
            return 0        # the batch got here before the drop; --ack picks up from it
        if deadline and time.time() + delay >= deadline:
            print(f"ttp: still cannot reach {entry.get('ssh') or entry['host']} and --timeout ran out; "
                  f"stopping (exit {LISTEN_GAVE_UP}). Nothing is lost: unacknowledged messages come "
                  f"back on the next listen", file=sys.stderr)
            return LISTEN_GAVE_UP
        if not told:
            until = " until --timeout runs out" if deadline else ""
            print(f"ttp: will keep retrying{until} and deliver messages once the connection is back",
                  file=sys.stderr)
            told = True
        time.sleep(delay)
        delay = min(delay * 2, 120.0)


def _ssh_cmd(entry: dict, argv: list[str]) -> list[str]:
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    cmd = " ".join(shlex.quote(a) for a in [remote_ttp, *argv])
    host = entry.get("ssh") or entry["host"]
    # Keepalives: a connection that died quietly (a laptop asleep, a changed network) ends within
    # about a minute instead of hanging until TCP gives up.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", host, cmd]


def _ssh(entry: dict, argv: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(_ssh_cmd(entry, argv), stdin=subprocess.DEVNULL if capture else None,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE, text=True)


def _ssh_relay(entry: dict, argv: list[str], shown: set[int]) -> subprocess.CompletedProcess:
    """Run argv there, passing its output through line by line, minus messages already in `shown`
    (a message line starts `[#<id> `; the lines after it belong to it)."""
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(_ssh_cmd(entry, argv), stdout=subprocess.PIPE, stderr=err, text=True)
        skip = False
        for line in proc.stdout:
            m = _MSG_LINE.match(line)
            if m:
                skip = int(m.group(1)) in shown
                shown.add(int(m.group(1)))
            if not skip:
                sys.stdout.write(line)
                sys.stdout.flush()
        rc = proc.wait()
        err.seek(0)
        return subprocess.CompletedProcess(proc.args, rc, None, err.read())


def _unreachable(entry: dict, stderr: str | None, rc: int = 255) -> None:
    host = entry.get("ssh") or entry["host"]
    why = ((stderr or "").strip().splitlines() or [f"ssh exit {rc}" if rc != 255 else "unknown ssh error"])[-1]
    print(f"ttp: cannot reach {host} right now ({why}). The project keeps running there; "
          f"try again once this machine is back on that network.", file=sys.stderr)
    from . import machines as mm
    if hint := mm.auth_failed_hint(stderr):
        print(f"ttp: {hint}", file=sys.stderr)


def forward(entry: dict, argv: list[str], quiet: bool = False, shown: set[int] | None = None) -> int:
    """Run this same command on the project's machine, streaming its output. With `shown` (a
    listener), message lines already printed are not printed again."""
    r = _ssh(entry, argv) if shown is None else _ssh_relay(entry, argv, shown)
    if shown is not None and REPLACED_MARK in (r.stderr or ""):
        # The far side said so before it went: whatever ssh reports (255, 143), this is no drop.
        sys.stderr.write(r.stderr)
        return LISTEN_REPLACED
    if r.returncode == 255:   # ssh itself failed: the project is fine, this machine cannot reach it
        if not quiet:
            _unreachable(entry, r.stderr)
    elif r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


def _send_say(entry: dict, argv: list[str], client_id: str, capture: bool = False) -> subprocess.CompletedProcess:
    """`ttp say` on the project's machine, tagged with the client id it deduplicates on."""
    r = _ssh(entry, [argv[0], f"--client-id={client_id}", *argv[1:]], capture)
    if r.returncode == 2 and "--client-id" in (r.stderr or ""):   # an older runtime there: send it untagged
        r = _ssh(entry, argv, capture)
    if r.returncode == 2 and "--context" in (r.stderr or "") and "--context" in argv:
        return _send_say(entry, _fold_context(argv), client_id, capture)
    return r


def _fold_context(argv: list[str]) -> list[str]:
    """The `ttp say` argv for a runtime without `--context`: the relayer's context goes after a
    relay.MARK line in the text, which that runtime keeps and a newer one splits off again."""
    from . import relay
    i = argv.index("--context")
    rest = argv[:i] + argv[i + 2:]
    return rest[:-1] + [f"{rest[-1]}\n\n{relay.MARK}\n{argv[i + 1]}"]


def flush_outbox(name: str, entry: dict, quiet: bool = False) -> subprocess.CompletedProcess | None:
    """Send what is queued on this machine for `name`, oldest first, each removed only once the
    project confirmed it. Returns the failed attempt (that message and the ones behind it stay
    queued), else None. Only a 255 means the project is unreachable; any other failure is that
    message's own problem and must not stop the caller's command."""
    if not outbox.entries(name):
        return None
    failed, sent = None, 0
    with outbox.locked(name):
        for e in outbox.entries(name):
            r = _send_say(entry, e["argv"], e["id"], capture=True)
            if r.returncode == 0:
                outbox.drop(name, e["id"])
                sent += 1
                continue
            why = ((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]
            if r.returncode == 2:     # the project refused it: set it aside, do not block the rest
                outbox.drop(name, e["id"], rejected=why)
                print(f"ttp: {name} refused queued message #{e['id']} ({why}); it is kept in "
                      f"{outbox.folder() / (name + '.rejected.jsonl')}", file=sys.stderr)
                continue
            failed = r
            if r.returncode != 255 and not quiet:
                print(f"ttp: queued messages for {name} stay queued ({why})", file=sys.stderr)
            break
    if sent:
        print(f"ttp: delivered {sent} queued message(s) to {name}", file=sys.stderr)
    return failed


def say_remote(name: str, entry: dict, text: str, chat: str | None, context: str = "") -> int:
    """Send a message to a project on another machine. If that machine cannot be reached, or older
    messages are still queued for it, queue this one here: it is never dropped, and goes out in order."""
    argv = ["say", name, *(["--chat", chat] if chat else []), *(["--context", context] if context else []),
            "--", text]
    cid = outbox.new_id()
    if flush_outbox(name, entry) is None and not outbox.entries(name):
        r = _send_say(entry, argv, cid)
        if r.returncode != 255:
            if r.stderr:
                sys.stderr.write(r.stderr)
            return r.returncode
    outbox.add(name, argv, cid, chat)
    print(f"queued on this machine; delivered once {name} is reachable (#{cid})")
    return 0


def search_transcripts(name: str) -> list[dict]:
    """Find a project's location markers in agent chat logs (Claude Code, Codex, Cursor)."""
    needle = f"tt-project://{name}@"
    roots = [Path.home() / ".claude" / "projects", Path.home() / ".codex" / "sessions", Path.home() / ".cursor" / "projects"]
    hits: dict[str, dict] = {}
    rx = re.compile(re.escape(needle) + r"([A-Za-z0-9._-]+):(/[^\s\"'\\]+)")
    for root in roots:
        if not root.is_dir():
            continue
        tool = shutil.which("rg")
        cmd = [tool, "-l", "-F", needle, str(root)] if tool else ["grep", "-rlF", needle, str(root)]
        try:
            files = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.split()
        except subprocess.SubprocessError:
            continue
        for f in files:
            try:
                text = Path(f).read_text(errors="replace")
            except OSError:
                continue
            for m in rx.finditer(text):
                hits[f"{m.group(1)}:{m.group(2)}"] = {"host": m.group(1), "dir": m.group(2).rstrip(".,)"),
                                                     "seen_in": f, "mtime": Path(f).stat().st_mtime}
    return sorted(hits.values(), key=lambda h: -h["mtime"])


def resolve(name: str) -> tuple[Project | None, dict | None]:
    p = local_project(name)
    if p:
        return p, None
    entry = remote_entry(name)
    if entry:
        return None, entry
    for hit in search_transcripts(name):
        if hit["host"] == hostname() and Project(hit["dir"]).exists():
            register(name, {"host": hostname(), "dir": str(Project(hit["dir"]).root)})
            return Project(hit["dir"]), None
        if hit["host"] != hostname():
            entry = {"host": hit["host"], "dir": hit["dir"]}
            register(name, entry)
            return None, entry
    return None, None


def need(name: str, argv: list[str]) -> Project:
    p, entry = resolve(name)
    if entry:
        if argv and argv[0] == "listen":
            sys.exit(forward_listen(entry, argv, name))
        failed = flush_outbox(name, entry)
        if failed is None or failed.returncode != 255:   # a message stuck for its own reason: run the command anyway
            rc = forward(entry, argv)
        else:
            _unreachable(entry, failed.stderr)
            rc = 255
        n = outbox.count(name)
        if n and argv[:1] == ["status"] and "--json" not in argv:
            print(f"{n} message(s) queued here for {name}; they go out once it is reachable")
        sys.exit(rc)
    if not p:
        die(f"no project named {name!r} on this machine or in the registry; try `ttp find {name}`")
    return p


# creating -----------------------------------------------------------------------------------------
def detect_provider() -> str:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("CURSOR_") for k in os.environ):
        return "cursor"
    return "claude"


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


SOURCE_FILE = "SOURCE_COMMIT"      # in runtime/ttp/: the git commit (or content hash) `ttp setup` installed this runtime from


def _checkout_commit(root: Path) -> str:
    """The commit of the git checkout the plugin at `root` is tracked in ("-dirty" with local edits
    under it), or "unknown" when it is not in one (a plugin cache, a copy, no git)."""
    def run(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
    if not run("ls-files", "--", "runtime/ttp/__init__.py"):      # not a checkout of this plugin
        return "unknown"
    commit = run("rev-parse", "--short=12", "HEAD")
    if not commit:
        return "unknown"
    return commit + ("-dirty" if run("status", "--porcelain", "--", ".") else "")


def recorded_commit(runtime: Path) -> str:
    """The source commit `ttp setup` recorded in an installed runtime, or "unknown"."""
    try:
        return (runtime / "ttp" / SOURCE_FILE).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def content_hash(runtime: Path) -> str:
    """'sha-<12 hex>' over the sorted relative paths and bytes of the files under `runtime` (no
    __pycache__, *.pyc or SOURCE_COMMIT): the identity of a runtime installed from no git checkout."""
    h = hashlib.sha256()
    for f in sorted(runtime.rglob("*"), key=lambda x: x.relative_to(runtime).as_posix()):
        rel = f.relative_to(runtime)
        if "__pycache__" in rel.parts or f.suffix == ".pyc" or rel.as_posix() == f"ttp/{SOURCE_FILE}" \
                or not f.is_file():
            continue
        data = f.read_bytes()
        h.update(f"{rel.as_posix()}\0{len(data)}\0".encode() + data)
    return "sha-" + h.hexdigest()[:12]


def source_commit() -> str:
    """The git commit this runtime came from: recorded at install, else read from the plugin checkout,
    else (a plugin cache or archive, no git) a content hash of the runtime."""
    got = recorded_commit(RUNTIME)
    if got == "unknown" and (PLUGIN_ROOT / "template").is_dir():   # a harness copy is not the plugin
        got = _checkout_commit(PLUGIN_ROOT)
        if got == "unknown":
            got = content_hash(RUNTIME)
    return got


# Template files installed at the harness root. pytest.ini isolates harness-local test runs from an
# enclosing repository's pytest config and conftest.py (pytest stops looking at the first config).
HARNESS_ROOT_FILES = ("pytest.ini",)
# Files that hold a pytest config at a directory's root, and the section that makes them one
_PYTEST_CONFIGS = {"pytest.ini": "", ".pytest.ini": "", "pytest.toml": "", ".pytest.toml": "",
                   "pyproject.toml": "[tool.pytest", "tox.ini": "[pytest]", "setup.cfg": "[tool:pytest]"}


def _own_pytest_config(h: Path) -> str:
    """The pytest config file main holds at the harness root that upstream never shipped, or "":
    the project's own, which an upgrade never overrides with the template's pytest.ini."""
    def show(ref: str, name: str) -> str | None:
        r = subprocess.run(["git", "-C", str(h), "show", f"{ref}:{name}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    if show("upstream", "pytest.ini") is not None:
        return ""
    for name, section in _PYTEST_CONFIGS.items():
        text = show("HEAD", name)
        if text is not None and section in text:
            return name
    return ""


def _runtime_version(runtime: Path) -> str:
    m = re.search(r'__version__ = "([^"]+)"', (runtime / "ttp" / "__init__.py").read_text())
    return m.group(1) if m else "unknown"


def bootstrap(root: Path, name: str, brief: str, provider: str, home_tz: str | None = None,
              home_from: str = "") -> Project:
    p = Project(root)
    if p.exists():
        if p.name != name:
            die(f"{p.base} already holds project {p.name!r}")
        return p
    p.base.mkdir(parents=True, exist_ok=True)
    # The folder ignores itself, so no enclosing repository can ever commit project state.
    durable_write(p.base / ".gitignore", "*\n")
    for d in (p.state, p.runs, p.logs, p.worktrees, p.memory_dir):
        d.mkdir(parents=True, exist_ok=True)
    template = PLUGIN_ROOT / "template"
    _copy_tree(RUNTIME, p.harness / "runtime")
    (p.harness / "runtime" / "ttp" / SOURCE_FILE).write_text(source_commit() + "\n")
    _copy_tree(template / "prompts", p.harness / "prompts")
    _copy_tree(template / "bin", p.harness / "bin")
    for f in (p.harness / "bin").iterdir():
        f.chmod(0o755)
    for f in HARNESS_ROOT_FILES:
        shutil.copy2(template / f, p.harness / f)
    (p.harness / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q", "-b", "upstream", str(p.harness)], check=True)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"tt-project template {__version__}")
    _git(p.harness, "checkout", "-q", "-b", "main")
    _ensure_git_ident(p.harness)
    from . import release
    release.guard_harness(p.harness)
    charter = (template / "CHARTER.md").read_text().replace("{{NAME}}", name).replace(
        "{{DATE}}", time.strftime("%Y-%m-%d")).replace("{{BRIEF}}", brief.strip() or "(no description given yet)")
    durable_write(p.charter_path, charter)
    durable_write(p.memory_index, "# Memory index\n")
    cfg = {"name": name, "created": time.time(), "root": str(p.root), "host": hostname(),
           "core_provider": provider, "tt_project_version": __version__,
           "id": pysecrets.token_hex(4),
           # New projects only: DEFAULT_CONFIG keeps it off, so existing projects are unchanged.
           "providers": {"claude": {"worker_isolation": True}}}
    # The user's zone: the workstation's when it sent one (new_remote), else this machine's. In an ssh
    # login with none sent this machine's is a server's: the fallback zone, set by no machine, so no
    # spend push moves it and the workstation's next connect sets it.
    if timefmt.valid(home_tz):
        cfg.update({timefmt.KEY: timefmt.valid(home_tz), timefmt.FROM_KEY: home_from or hostname()})
    elif timefmt.in_ssh_login():
        cfg[timefmt.KEY] = timefmt.fallback()[0]
    else:
        cfg.update({timefmt.KEY: timefmt.detect_local(), timefmt.FROM_KEY: hostname()})
    sec = load_secrets()
    if (sec.get("slack") or {}).get("bot_token"):
        cfg["notify"] = {"slack": True}
    write_json(p.config_path, cfg)
    _git(p.harness, "add", "-A")
    _git(p.harness, "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost", "commit", "-q",
         "-m", f"project {name}: charter and config")
    db = p.db
    for s in json.loads((template / "recurring.json").read_text()):
        sched.upsert(db, s["name"], s["kind"], s["every"], s.get("at"), s.get("enabled", True),
                     s.get("budget_usd_day"), s.get("description", ""), s.get("payload", {}), cfg[timefmt.KEY])
    sched.write_file(p, "schedules: from the template", create=True)
    db.set_meta("name", name)
    db.post("in", "Project created. Brief:\n" + (brief.strip() or "(none)") +
            "\n\nRead the charter, restate the goals, success criteria and restrictions as you understand "
            "them, list what you still need to know, and start the first tasks that do not depend on answers.",
            chat=None, channel="system", kind="user", provenance="system")
    return p


def cmd_new(a) -> None:
    if not NAME_RE.match(a.name):
        die("project names use letters, digits, '.', '_' and '-' (max 63)")
    brief = a.describe or ""
    if a.describe_file:
        brief = sys.stdin.read() if a.describe_file == "-" else Path(a.describe_file).expanduser().read_text()
    if a.host and a.host not in (hostname(), "localhost"):
        return new_remote(a, brief)
    root = Path(a.dir).expanduser().resolve() if a.dir else _default_root()
    p = bootstrap(root, a.name, brief, a.provider or detect_provider(), a.home_tz, a.home_from or "")
    register(a.name, {"host": hostname(), "dir": str(p.root)})
    ident = subprocess.run(["git", "-C", str(p.root), "config", "user.email"], capture_output=True, text=True)
    if ident.returncode != 0 or not ident.stdout.strip():
        print("warning: git has no user.email here, so workers cannot commit. Set one "
              "(git config --global user.name/user.email) or tell the coordinator which identity to use.")
    how = "service not installed (--no-service)"
    if not a.no_service:
        from .service import install
        how = install(p)
    _wait_for_daemon(p)
    print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    print(f"created {a.name} in {p.base} · daemon via {how}")
    from . import weblink
    line, rc = weblink.local(p, weblink.link(p))
    print(line)
    if rc:
        sys.exit(rc)


def _default_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip() if out.returncode == 0 else os.getcwd())


def push_secrets(host: str) -> str:
    """Copy this user's saved keys to another machine (same user), merging with what is there.
    Travels over ssh stdin, lands mode 0600, never on a command line or in a project folder."""
    sec = load_secrets()
    if not sec:
        return "no saved keys to copy"
    merge = ("import json,os,sys;h=os.path.expanduser('~/.tt-project');os.makedirs(h,mode=0o700,exist_ok=True);"
             "p=os.path.join(h,'secrets.json');old={}\n"
             "try: old=json.load(open(p))\nexcept Exception: pass\n"
             "new=json.load(sys.stdin);new.update({k:v for k,v in old.items() if k not in new});"
             "fd=os.open(p+'.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(fd,json.dumps(new).encode());"
             "os.close(fd);os.replace(p+'.tmp',p);print(','.join(sorted(new)))")
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"python3 -c {shlex.quote(merge)}"],
                       input=json.dumps(sec), text=True, capture_output=True)
    return f"copied keys ({r.stdout.strip()}) to {host}" if r.returncode == 0 else f"could not copy keys: {r.stderr[-200:]}"


def remote_version(host: str) -> str:
    """The version of the `ttp` installed on another machine ("" when none or unreadable)."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "cat ~/.tt-project/lib/current/runtime/ttp/__init__.py"],
                       capture_output=True, text=True)
    m = re.search(r'__version__ = "([^"]+)"', r.stdout) if r.returncode == 0 else None
    return m.group(1) if m else ""


def ship_runtime(host: str) -> str:
    """Copy this runtime and template to ~/.tt-project/lib/<version> on another machine and make it
    that machine's installed `ttp`. Returns the remote launcher path. A machine that already has
    this version or a newer one keeps its own: an older runtime never replaces a newer install."""
    from .release import is_newer
    there = remote_version(host)
    if there and not is_newer(__version__, there):
        return "~/.tt-project/lib/current/bin/ttp"
    stage = f".tt-project/lib/{__version__}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"rm -rf ~/{stage} && mkdir -p ~/{stage}"])
    mac = ["--no-xattrs", "--no-mac-metadata"] if sys.platform == "darwin" else []
    tar = subprocess.Popen(["tar", *mac, "--exclude", "__pycache__", "-C", str(PLUGIN_ROOT), "-czf", "-",
                            "runtime", "template", "bin"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env={**os.environ, "COPYFILE_DISABLE": "1"})
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"tar -C ~/{stage} -xzf -"], stdin=tar.stdout)
    tar.wait()
    mark = f"printf '%s\\n' {shlex.quote(source_commit())} > ~/{stage}/runtime/ttp/{SOURCE_FILE}"
    subprocess.check_call(["ssh", "-o", "BatchMode=yes", host, f"{mark} && ~/{stage}/bin/ttp setup >/dev/null"])
    return f"~/{stage}/bin/ttp"


def new_remote(a, brief: str) -> None:
    """Ship this runtime to the other machine and create the project there."""
    if not a.dir:
        die("--dir is required with --host (the project root on that machine)")
    host = a.host
    launcher = ship_runtime(host)
    if load_secrets() and not a.no_secrets:
        print(push_secrets(host))
    from . import machines as mm
    print(mm.push(host))        # its daemon reads the machines list there
    args = [launcher, "new", a.name, "--dir", a.dir, "--provider", a.provider or detect_provider()]
    # This workstation's zone, never the box's (that runtime is at least this one: ship_runtime). In an
    # ssh login here it is a server's, so none: the box falls back and the next workstation connect sets it.
    if not timefmt.in_ssh_login():
        args += ["--home-tz", timefmt.detect_local(), "--home-from", hostname()]
    if a.no_service:
        args.append("--no-service")
    remote = " ".join(shlex.quote(x) if not x.startswith("~/") else x for x in args) + " --describe-file -"
    from . import weblink
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], input=brief, text=True, stdout=subprocess.PIPE)
    _echo_without_web(r.stdout)
    if r.returncode not in (0, weblink.UNVERIFIED):   # UNVERIFIED: created, but its web app failed the check there
        die(f"remote creation on {host} failed", r.returncode)
    entry = {"host": host, "dir": a.dir}
    register(a.name, entry)
    line, rc = remote_web(a.name, entry, r.stdout)
    print(line)
    if rc:
        sys.exit(rc)


def _echo_without_web(out: str | None) -> None:
    """Print a remote command's output without its web app line: that link is for the far machine's
    own localhost, and the caller prints the one that works here."""
    for ln in (out or "").splitlines():
        if not ln.startswith("web app:"):
            print(ln)


def remote_web(name: str, entry: dict, out: str | None) -> tuple[str, int]:
    """The checked local link to a remote project's web app, read from the web line its own `ttp new`
    or `ttp connect` printed there (or asked for when an older runtime printed none)."""
    from . import weblink
    host = entry.get("ssh") or entry["host"]
    there = next((ln for ln in (out or "").splitlines() if ln.startswith("web app:")), "")
    if "NOT AVAILABLE" in there:    # its own check and repair on that machine failed: nothing to tunnel to
        return f"{there} (on {host})", weblink.UNVERIFIED
    m = weblink.LINK.search(there)
    if not m:
        r = _ssh(entry, ["web", name], capture=True)
        m = weblink.LINK.search(r.stdout or "")
        if not m:
            why = ((r.stderr or r.stdout or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]
            return weblink.bad_line("web", f"could not read the web address from {host} ({why})", []), weblink.UNVERIFIED
    return weblink.remote(name, entry, int(m.group(1)), m.group(2))


def _wait_for_daemon(p: Project, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (p.db.kv("web") or {}).get("port"):
            return
        time.sleep(0.5)


def web_line(p: Project) -> str:
    from .weblink import link
    url = link(p)
    return f"web app: {url}" if url else "web app: not running yet (check `ttp status`)"


# talking ------------------------------------------------------------------------------------------
def cmd_connect(a) -> None:
    p, entry = resolve(a.name)
    if entry:
        argv = ["connect", a.name, *(["--chat", a.chat] if a.chat else []), *(["--label", a.label] if a.label else [])]
        sys.exit(connect_remote(a.name, entry, argv))
    p = need(a.name, sys.argv[1:])
    # The home zone follows the user: the zone a workstation sent, or this machine's own when the
    # user types here (an ssh login here is a server's zone, not the user's).
    tz, src = a.home_tz, a.home_from or ""
    if not tz and not timefmt.in_ssh_login():
        tz, src = timefmt.detect_local(), hostname()
    if tz:
        timefmt.set_home(p, tz, src, f"ttp connect from {src}" if src else "ttp connect")
    chat = a.chat or f"c{pysecrets.token_hex(3)}"
    p.db.x("INSERT INTO chats(id,created,label,host,last_active,last_read) VALUES(?,?,?,?,?,"
           "(SELECT COALESCE(MAX(id),0) FROM messages)) ON CONFLICT(id) DO UPDATE SET last_active=excluded.last_active",
           (chat, time.time(), a.label or "", os.environ.get("TTP_CLIENT_HOST", ""), time.time()))
    print(MARKER.format(name=p.name, host=hostname(), dir=p.root))
    print(f"chat: {chat}")
    print(status_text(p))
    from . import weblink
    line, rc = weblink.local(p, weblink.link(p))
    print(line)
    if rc:
        sys.exit(rc)


def connect_remote(name: str, entry: dict, argv: list[str]) -> int:
    """`ttp connect` on the project's machine, then the checked link through the kept local forward."""
    from . import weblink
    failed = flush_outbox(name, entry)
    if failed is not None and failed.returncode == 255:
        _unreachable(entry, failed.stderr)
        return 255
    # The user's zone only from their workstation: in an ssh login here it is a server's, so none.
    home = [] if timefmt.in_ssh_login() else ["--home-tz", timefmt.detect_local(), "--home-from", hostname()]
    r = _ssh(entry, [*argv, *home], capture=True)
    if home and r.returncode == 2 and "--home-tz" in (r.stderr or ""):   # an older runtime there: connect without it
        r = _ssh(entry, argv, capture=True)
    if r.returncode == 255:
        _unreachable(entry, r.stderr)
        return 255
    if r.stderr:
        sys.stderr.write(r.stderr)
    _echo_without_web(r.stdout)
    if r.returncode not in (0, weblink.UNVERIFIED):
        return r.returncode
    line, rc = remote_web(name, entry, r.stdout)
    print(line)
    return rc


def cmd_say(a) -> None:
    if in_run():
        # A message posted here counts as the user's (pr_approve reads approvals from it), so a run
        # must not post one. Runs report through `ttp note` and their hand-off.
        die("ttp say posts a message as the user and is refused inside a run; use `ttp note` or the hand-off")
    from . import relay
    p, entry = resolve(a.name)
    # Only the user's verbatim words are the message; what a relayer adds is kept apart (relay.py).
    text, context = relay.split(a.text if a.text != "-" else sys.stdin.read(), a.context)
    if not text:
        die("empty message" if not context else "no user's words: --context alone is not a message")
    if entry:
        sys.exit(say_remote(a.name, entry, text, a.chat, context))
    p = p or need(a.name, sys.argv[1:])
    seen = None
    with p.db.tx():   # a client id makes a resent message (its confirmation was lost) a no-op
        if a.client_id:
            seen = p.db.one("SELECT id FROM messages WHERE direction='in' AND ref=?", (f"client:{a.client_id}",))
        mid = seen["id"] if seen else p.db.post("in", text.strip(), chat=a.chat or None, channel="chat",
                                                 kind="user", ref=f"client:{a.client_id}" if a.client_id else None,
                                                 provenance="cli-legacy", context=context)
        if a.chat:
            p.db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
    if seen:
        print(f"already received (#{mid})")
    else:
        print(f"sent (#{mid}); the coordinator answers in this chat when it has decided")


def cmd_listen(a) -> None:
    p = need(a.name, sys.argv[1:])
    db = p.db
    if a.ack is not None:
        # Acknowledged ids only move forward and never past the newest message.
        db.x("UPDATE chats SET last_read=MAX(COALESCE(last_read,0), MIN(?, (SELECT COALESCE(MAX(id),0) "
             "FROM messages))) WHERE id=?", (a.ack, a.chat))
    row = db.one("SELECT last_read, min_severity FROM chats WHERE id=?", (a.chat,))
    if not row:
        die(f"unknown chat {a.chat}; run `ttp connect {a.name}` first")
    floor = chat_floor(row["min_severity"], p.config()["notify"].get("chat_min_severity"))
    after = int(row["last_read"] or 0)
    # One listener per chat, and the newest wins. Two would race for the same messages, and one
    # printing to nowhere (a lost background task, a dropped ssh session) would silently mark them
    # read. Whoever arms a listener last is the one that wants the messages.
    lock = p.state / f"listen-{a.chat}.pid"
    try:
        other = int(lock.read_text())
    except (OSError, ValueError):
        other = 0
    # Claim the chat before stopping the older listener: while it shuts down, a third listener
    # must find this one in the lock, and the older one's cleanup must not remove it.
    tmp = lock.with_name(f"{lock.name}.{os.getpid()}")
    tmp.write_text(str(os.getpid()))
    os.replace(tmp, lock)
    if other and other != os.getpid() and _listener_alive(other, a.chat):
        try:
            os.kill(other, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not _listener_alive(other, a.chat):
                break
            time.sleep(0.1)
        print(f"ttp: replaced an older listener for chat {a.chat} (pid {other})", file=sys.stderr)
    _on_replaced(lock, a.chat)
    try:
        _listen_loop(p, db, a, after, floor)
    finally:
        try:
            if lock.read_text().strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def _on_replaced(lock: Path, chat: str) -> None:
    """On SIGTERM from a newer listener (it claimed the lock first), say so on stderr and exit
    LISTEN_REPLACED, so a forwarder on another machine stops instead of reconnecting and taking
    the chat back. Any other SIGTERM ends the listener as before."""
    def handler(signum, frame):
        try:
            newer = int(lock.read_text())
        except (OSError, ValueError):
            newer = 0
        if newer and newer != os.getpid() and _pid_alive(newer):
            print(f"{REPLACED_MARK} for chat {chat} (pid {newer}); this one stops without reconnecting "
                  f"(exit {LISTEN_REPLACED})", file=sys.stderr, flush=True)
            sys.exit(LISTEN_REPLACED)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    signal.signal(signal.SIGTERM, handler)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _listener_alive(pid: int, chat: str) -> bool:
    """True if pid is a live `ttp listen` for this chat (not a recycled pid)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if zombie(pid):
        return False
    try:
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "listen" in cmd and chat in cmd


def _listen_loop(p: Project, db, a, after: int, floor: str) -> None:
    from .web import cleared
    deadline = time.time() + a.timeout if a.timeout else None
    parent = os.getppid()
    while True:
        if os.getppid() != parent:
            return      # whoever started this listener is gone; nobody would read what it prints
        # The high-water mark comes first: a reply posted after it is left for the next pass,
        # never skipped as if it had been read.
        top = db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"]
        msgs = [m for m in db.unread_for_chat(a.chat, after, floor, upto=top) if not cleared(db, m, time.time())]
        for m in msgs:
            kind = f"ask {m['id']}" if m["kind"] == "ask" else m["kind"]   # "ask N": "#N" reads as a task
            who = "coordinator" if m["chat"] else f"{p.name} ({kind}, {m['severity']})"
            print(f"[#{m['id']} {who}] {m['text']}", flush=True)
        if top > after:
            after = top
            # With --ack, only an acknowledgement marks messages read, so what was printed to a
            # reader that is gone comes back on the next listen.
            if a.ack is None:
                db.x("UPDATE chats SET last_read=? WHERE id=?", (after, a.chat))
        if msgs:
            db.x("UPDATE chats SET last_active=? WHERE id=?", (time.time(), a.chat))
            if a.once:
                return
        if deadline and time.time() > deadline:
            return
        time.sleep(poll_s(2))


def status_text(p: Project) -> str:
    """One screen: what needs the user now first, then the budget and the work, then information."""
    from .alerts import feed
    from .web import at, attention, health, since
    db = p.db
    state = daemon_state(p)
    h = health(p, db, alive=state == "running")
    now = time.time()
    tz = timefmt.zone(p)
    counts = db.status_counts()
    head = f"{p.name}: daemon {state}" + ((" (paused until reboot)" if db.kv("pause_until_boot") else " (paused)")
                                          if db.kv("paused") else "")
    head += " · tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "none yet")
    head += " · " + timefmt.long(now, p)
    lines = [head]
    if h["runtime"]["line"]:
        lines.append(h["runtime"]["line"])
    for m in attention(db, now)[:6]:
        text = " ".join(m["text"].split())
        what = f"ask {m['id']}" if m["kind"] == "ask" else "alert"
        lines.append(f"  needs you ({what}, {since(m['ts'], now)} ago): {text[:300]}")
    # Blocked tasks: those that wait on the user join the top; the rest are the project's to move.
    from . import waits
    you, stuck = waits.blocked(db, now, tz)
    for t in you[:6]:
        lines.append(f"  needs you (#{t['id']}, {t['wait']['age']}): {t['title']} — {t['wait']['text']}")
    lines.append("budget: " + h["spend"]["headline"])
    c = h["coordinator"]
    coord = "coordinator: no turn yet"
    if c["last_turn"]:
        coord = f"coordinator: last turn {int((now - c['last_turn']) // 60)} min ago" + (
            f" ({c['last_status']})" if c["last_status"] else "")
    if c["failures"]:
        coord += f" · {c['failures']} failed in a row"
    if c["backoff_until"]:
        coord += f" · retry at {at(c['backoff_until'], now, tz)}"
    if c["idle_wake"]:
        coord += f" · next idle check {at(c['idle_wake'], now, tz)}"
    elif c["idle_held"]:
        coord += f" · idle check {c['idle_held']}"
    lines.append(coord)
    from . import budget
    cache = budget.coordinator_cache(db, now)["24h"]
    if cache["turns"]:
        lines.append(budget.coordinator_cache_line(cache))
    from . import pushq
    pq = pushq.status_line(p, now)
    if pq:
        lines.append(pq)
    for b in h.get("breakers") or []:
        lines.append(b["line"])
    for pp in h["providers_paused"]:
        lines.append(f"{pp['provider']} paused until {at(pp['until'], now, tz)}: {pp['note']} — fix: {pp['fix']}")
    for pr in h["resources_paused"]:
        lines.append(f"resource {pr['resource']} paused since {at(pr['since'], now, tz)} by {pr.get('by') or 'user'}"
                     + (f" in {pr['project']} (shared: holds in every project)" if pr.get("shared") else "")
                     + (f": {pr['reason']}" if pr.get("reason") else "")
                     + f" — resume: ttp resume {p.name} --resource {pr['resource']}")
    from . import locks, shared
    for res in sorted(shared.names(p.config())):
        who = locks.held(shared.root() / res, p)
        lines.append(f"shared {res}: " + ("; ".join(w.split(": ", 1)[-1] for w in who) if who else "free"))
    for res, got in shared.mismatches(p).items():
        lines.append(f"shared {res}: projects give different slot counts ("
                     + ", ".join(f"{k} {n}" for k, n in sorted(got.items())) + f"); all use {min(got.values())}")
    if h["why_idle"]:
        lines.append(f"idle: {h['why_idle']}")
    elif h["held"]:
        lines.append(f"held: {h['held']}")
    for w in h["working"][:8]:
        what = f"#{w['task']} {w['title']}" if w["task"] else w["role"]
        lines.append(f"  running {since(w['started'], now)}: {what}" + (f" ({w['wake']} wake)" if w.get("wake") else "")
                     + (f" — {w['note']}" if w["note"] else ""))
    in_review = db.review_since()
    if stuck:
        lines.append("stuck, the project is on it:")
    for t in stuck[:8]:
        lines.append(f"  #{t['id']} {t['wait']['age']}: {t['title']} — {t['wait']['text']}")
    for t in db.q("SELECT id,title,status,blocked_reason FROM tasks WHERE status IN ('review','pushing') "
                  "ORDER BY status, id LIMIT 8"):
        age = f" {since(in_review[t['id']], now)}" if t["id"] in in_review else ""
        lines.append(f"  #{t['id']} {t['status']}{age}: {t['title']}" + (f" — {t['blocked_reason']}" if t["blocked_reason"] else ""))
    if h.get("schedules_broken"):
        lines.append(h["schedules_broken"])
    if h.get("schedules_waiting"):
        lines.append(h["schedules_waiting"])
    if h.get("health"):
        lines.append(h["health"])
    if h.get("coverage"):
        lines.append(h["coverage"])
    lines.extend(h.get("obligations") or [])
    from . import screen as scr
    for m in scr.mutes(db, now):
        lines.append(f"muted: {scr.mute_line(m, now, p)}")
    if h.get("host"):
        lines.append(h["host"])
    if h.get("idle_sleep"):
        lines.append(h["idle_sleep"])
    if h.get("release"):
        lines.append(h["release"])
    if h.get("local_only"):
        lines.append(h["local_only"])
    if h.get("uncommitted"):
        lines.append(h["uncommitted"])
    if h.get("upstream"):
        lines.append(h["upstream"])
    disk = db.kv("disk_low")
    if disk:
        lines.append(f"disk: only {disk['free_gb']} GB free under {disk['path']} (guard {disk.get('threshold_gb', '?')} GB); "
                     f"only questions and plans start")
    for t in h["logged_out"][:5]:
        lines.append(f"  #{t['id']} held: logged out: {t['title']}")
    for t in h.get("net_held", [])[:5]:
        lines.append(f"  #{t['id']} held: network: {t['title']}")
    for t in h["waiting"][:5]:
        why = re.sub(r";? *next try \S+$", "", t["blocked_reason"] or "").strip()
        lines.append(f"  #{t['id']} waiting, next try {at(t['not_before'], now, tz)}: {t['title']}" + (f" — {why}" if why else ""))
        lines += [f"    job {j['name']}: {j['line']}" for j in (db.kv("job_use", {}) or {}).values() if j.get("task") == t["id"]]
    from .coordinator import BROKEN_START
    for t in h["deferred"][:5]:
        broken = t["starts"].startswith(BROKEN_START + ":")
        lines.append(f"  #{t['id']} {t['starts'] if broken else 'deferred, ' + t['starts']}: {t['title']}")
    if h["undelivered"]:
        u = h["undelivered"]
        lines.append(f"chat relay: {u['asks']} question(s) not delivered to any chat since {at(u['since'], now, tz)}; "
                     f"is the chat's `ttp listen` running?")
        if u["below_floor"]:
            lines.append(f"  {u['below_floor']} of them are below every chat's severity floor; lower "
                         f"notify.chat_min_severity or the chat's own floor to see them")
    recent = feed(db, now, limit=3)
    if recent:
        lines.append("recent:")
    for m in recent:
        text = " ".join(m["text"].split())
        tag = f"cleared {at(m['cleared_at'], now, tz)}" if m.get("cleared_at") else m["state"]
        lines.append(f"  {at(m['ts'], now, tz)} ({tag}) {text[:160]}")
    return "\n".join(lines)


def daemon_state(p: Project) -> str:
    """'running', or NOT RUNNING with why: its process is gone, or it stopped completing ticks."""
    from .daemon import HEARTBEAT_STALE_S, _alive, heartbeat
    d = p.db.kv("daemon", {})
    alive = bool(d.get("pid")) and d.get("host") == hostname() and _alive(int(d["pid"]))
    hb = heartbeat(p)
    if not alive:
        return "NOT RUNNING"
    if hb and int(hb.get("pid") or 0) == int(d["pid"]) and hb["age"] > HEARTBEAT_STALE_S:
        return f"NOT RUNNING (process {d['pid']} is up but stuck: no completed tick for {int(hb['age'] // 60)} min)"
    return "running"


def here() -> Project | None:
    """The project this process runs in: its run's project, else the folder it was started in."""
    if os.environ.get("TTP_PROJECT") and Project(os.environ["TTP_PROJECT"]).exists():
        return Project(os.environ["TTP_PROJECT"])
    return next((c for d in [Path.cwd(), *Path.cwd().parents] if (c := Project(d)).exists()), None)


def cmd_status(a) -> None:
    if a.name:
        p = need(a.name, sys.argv[1:])
    else:
        p = here()
        if not p:
            die("no tt-project project here; name one: `ttp status <name>`")
    if a.json:
        from .web import state_payload
        print(json.dumps(state_payload(p, p.db), default=str, indent=1))
    else:
        print(status_text(p))


def cmd_stats(a) -> None:
    """Context re-read (cache-read) tokens per run and per $, by role, kind, tier and effort."""
    from . import budget as bud
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp stats <name>`")
    s = bud.reread_stats(p.db, a.days, top=a.top)
    print(json.dumps(s, indent=1) if a.json else bud.reread_text(s))


def cmd_audit(a) -> None:
    """The daily review's self-efficiency audit of this project's last `--hours`, model-free."""
    from . import audit
    p = need(a.name, sys.argv[1:]) if a.name else here()
    if not p:
        die("no tt-project project here; name one: `ttp audit <name>`")
    hours = float((p.config().get("review") or {}).get("blocked_long_h", audit.BLOCKED_H))
    if a.json:
        print(json.dumps(audit.report(p.db, window_s=a.hours * 3600, blocked_h=hours, p=p), indent=1, default=str))
    else:
        print("\n".join(f"- {line}" for line in audit.lines(p.db, window_s=a.hours * 3600, blocked_h=hours, p=p)))


def cmd_spend_today(a) -> None:
    """This machine's projects' spend in [since, until) by provider and account key, and its other
    Claude Code sessions as estimated (globalcap.answer): what another machine's global daily total
    asks for over ssh (globalcap.fetch). The account itself stays here.
    `--receive` keeps what a machine that cannot be asked pushes here instead (globalcap.push)."""
    from . import globalcap as gcap
    now = time.time()
    if a.receive:
        ack, rc = gcap.receive(sys.stdin.buffer.read(gcap.RECEIVE_BYTES + 1), a.via or "", now)
        if rc == 0 and ack.get("tz"):
            # The sender's zone moves the home zone of this machine's projects that it set last.
            import sqlite3
            for e in load_registry().get("projects", {}).values():
                if e.get("host") in (hostname(), "localhost") and e.get("dir") and Project(e["dir"]).exists():
                    try:
                        timefmt.follow_push(Project(e["dir"]), ack["tz"], ack.get("from") or "")
                    except (OSError, RuntimeError, sqlite3.Error):
                        pass
            ack = {k: v for k, v in ack.items() if k not in ("tz", "from")}
        print(json.dumps(ack, sort_keys=True))
        raise SystemExit(rc)
    if a.since is None:
        b = deep_merge(DEFAULT_CONFIG["budget"], load_account_settings().get("budget") or {})
        a.since, a.until, _ = gcap.window(b, now)
    t = gcap.answer(a.since, a.until if a.until is not None else now + gcap.DAY, now=now)
    if a.json:
        print(json.dumps(t))
        return
    o = t.get(gcap.SESSIONS)
    other = (f", other Claude Code sessions {gcap.money(o['usd'])} (estimated)" if o
             else ", other Claude Code sessions unknown")
    print(f"{t['host']}: {len(t['projects'])} projects, {gcap.money(sum(r['usd'] for r in t['rows']))}{other} since "
          f"{time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(a.since))}" +
          "".join(f"\n  could not read {e}" for e in t["errors"]))


def cmd_web(a) -> None:
    from . import tunnel, weblink
    if a.unkeep:
        print(tunnel.unkeep(a.name))
        return
    if a.keep and not a.tunnel:
        die("--keep goes with --tunnel: `ttp web NAME --tunnel --keep`")
    p, entry = resolve(a.name)
    if p:
        if a.keep:
            print(f"{a.name} runs on this computer: there is no tunnel to keep")
        line, rc = weblink.local(p, weblink.link(p))
        print(line)
        if rc:
            sys.exit(rc)
        return
    if not entry:
        die(f"no project {a.name!r}")
    host = entry.get("ssh") or entry["host"]
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, f"{remote_ttp} web {shlex.quote(a.name)}"],
                       capture_output=True, text=True)
    m = weblink.LINK.search(r.stdout)
    if not m:
        die(f"could not read the web address from {host}: {(r.stderr or r.stdout).strip()[-200:]}")
    from .web import free_port
    remote_port, tok = int(m.group(1)), m.group(2)
    kept = tunnel.installed(a.name)
    if a.keep:
        line, rc = weblink.remote(a.name, entry, remote_port, tok)
        print(line)
        if rc:
            sys.exit(rc)
        return
    opened = False
    if kept and kept["host"] == host and kept["remote"] == remote_port and kept["local"]:
        local, opened = kept["local"], True   # the kept tunnel already forwards; a second one would only clash
        print(f"the kept tunnel forwards localhost:{local} → {host}:{remote_port} ({kept['file']})")
    else:
        local = free_port(remote_port + 100)
        cmd = tunnel.ssh_argv(host, local, remote_port, ssh="ssh")
        if a.tunnel:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            print(f"tunnel open (pid in background): localhost:{local} → {host}:{remote_port}")
            opened = True
        else:
            print(f"The project runs on {host}. `ttp web {a.name} --tunnel --keep` opens this local forward and "
                  f"keeps it up:\n  {' '.join(cmd)}")
    url = f"http://127.0.0.1:{local}/#token={tok}"
    kind, why = weblink.verify(url, a.name) if opened else ("", "")
    if kind:
        print(weblink.bad_line(kind, why, []))
        sys.exit(weblink.UNVERIFIED)
    print(weblink.ok_line(url) if opened else f"web app (once that forward is open): {url}")


def cmd_note(a) -> None:
    # Model-free completion commands get only note context, never a full worker context.
    run_dir = os.environ.get("TTP_RUN_DIR") or os.environ.get(NOTE_RUN_ENV)
    if not run_dir:
        die("ttp note only works inside a tt-project run")
    if a.to:
        _note_to(a, run_dir)
    durable_append(Path(run_dir) / "progress.md", f"{time.strftime('%H:%M:%S')} {a.text}\n")


def cmd_notify(a) -> None:
    """`ttp notify`: a worker's message for the user. The run cannot reach the daemon's database, so
    it is appended to the run's notify file, and the daemon posts it as an alert when the run ends
    (daemon.relay_worker_notifies): low or normal severity only, a few per run, an identical text
    once. High severity stays the coordinator's."""
    from .daemon import NOTIFY_FILE
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp notify only works inside a tt-project run")
    text = a.text.strip()
    if not text:
        die("empty notify")
    durable_append(Path(run_dir) / NOTIFY_FILE, json.dumps({"ts": time.time(), "severity": a.severity,
                                                           "text": text}) + "\n")
    durable_append(Path(run_dir) / "progress.md", f"{time.strftime('%H:%M:%S')} notify: {text[:200]}\n")
    print("queued for the user: it is sent when this run ends; say so in the hand-off")


def _note_to(a, run_dir: str) -> None:
    """`ttp note --to <project>`: a worker's note for another project, filed in the user's upstream
    inbox with this run's project and task (upstream.send); one on another machine is sent on there by
    the daemon (upstream.forward). Nothing of the other project's is written; its daemon reads the
    note as untrusted data from this worker. A note past upstream.SPEC_CHARS once whitespace is joined
    is refused whole, never cut; a filed or queued one prints the stored length and a short hash."""
    from . import upstream
    base = os.environ.get("TTP_PROJECT")
    me = Project(base) if base else None
    if not me or not me.exists():
        die("ttp note --to: this run has no project (TTP_PROJECT)")
    if a.to == me.name:
        die("ttp note --to names this run's own project; report to it with `ttp note` and the hand-off")
    far = remote_entry(a.to)
    targets = upstream.forward_to()
    if far and targets is not None and (far.get("ssh") or far["host"]) not in targets:
        die(f"project {a.to} runs on another machine, and upstream.forward_to does not send notes there. "
            f"Put the note in the hand-off as a follow-up titled `upstream: ...` instead")
    if not far and not local_project(a.to):
        die(f"unknown project {a.to}; `ttp list` shows the projects on this machine")
    if not a.text.strip():
        die("empty note")
    try:
        text = upstream.note_text(a.text)
    except upstream.NoteTooLong as e:
        die(f"not sent: {e}. Nothing was filed or cut. Shorten it, with the question or request first, "
            f"or put the whole text in the hand-off as a follow-up")
    sent = f"; stored {upstream.receipt(text)}"
    task = os.environ.get("TTP_TASK") or ""
    try:
        got, place = upstream.send_or_queue(me.state, me.name, int(task) if task.isdigit() else None, a.to,
                                            text, a.severity)
    except OSError as e:
        # A sandbox that cannot write the inbox (read-only home): the run's directory keeps the note,
        # and this project's daemon files it when the run ends (upstream.send_queued).
        upstream.queue(Path(run_dir), a.to, text, a.severity)
        print(f"note for {a.to} queued in this run's directory ({e.strerror or e}); this project's daemon files "
              f"it in its inbox as {upstream.note_id(a.to, text)} when the run ends{sent}")
        return
    if got == "full":
        die(f"not sent: {upstream.OUTBOX_MAX} notes of this project already wait to go to other projects. "
            f"Put it in the hand-off instead and keep working")
    if got == "queued":
        print(f"note for {a.to} queued: number {place} in this project's outbox (other projects get at most "
              f"{upstream.NOTES_PER_HOUR} of its notes an hour); its daemon files it as "
              f"{upstream.note_id(a.to, text)} when the limit allows{sent}. Nothing to wait for: keep working")
        return
    print(f"note for {a.to} " + ("already in its inbox" if got == "duplicate" else "filed in its inbox")
          + f" as {upstream.note_id(a.to, text)}" + sent
          + (f"; this machine's daemon sends it on to {far.get('ssh') or far['host']}" if far else ""))


def cmd_upstream(a) -> None:
    """The user's upstream inbox across machines: `--receive` files notes another machine sends over
    ssh (JSON lines on stdin; prints one JSON ack line), `--forward-status` shows each target this
    machine sends its notes on to, and `--forward-to` sets those targets (`default`: the machines of
    the remote projects; `none`: send nothing)."""
    from . import upstream
    if a.receive:
        ack, rc = upstream.receive(sys.stdin.buffer.read(upstream.RECEIVE_BYTES + 1), a.via or "")
        print(json.dumps(ack, sort_keys=True))
        raise SystemExit(rc)
    if a.forward_to is not None:
        v = a.forward_to.strip()
        try:
            upstream.set_forward_to(None if v == "default" else [] if v == "none"
                                    else [x for x in v.replace(",", " ").split() if x])
        except ValueError as e:
            die(str(e))
        got = upstream.forward_to()
        print("upstream notes go to " + ("the machines of the remote projects" if got is None
                                         else ", ".join(got) if got else "no other machine"))
        return
    for line in upstream.forward_status():
        print(line)

def cmd_push(a) -> None:
    """Publish this worktree's commits onto the project's target branch, guarded: refuse a dirty
    tree, rebase onto the latest tip, run `delivery.push_checks` on the final head, start over if
    the tip moved meanwhile, and push without force. The target is `delivery.push_branch`, never
    main, master or the remote's default branch. Pushes to one branch take turns; one that waits
    longer than `delivery.push_wait_s` (unset: twice the last check run, 900 s to 2 h) for its
    turn exits 75. With `delivery.version_bump` it bumps the version after each rebase. `--free` only tells whether it is
    free (0) or taken (1). `--detach` runs the push in a process of its own and prints its marker and
    a `--result <marker>` probe (0 once finished or dead, 1 while running). `--own` publishes the
    task's own `ttp/t<id>-...` branch under its own name instead, as it is: checks run on HEAD, no
    rebase or bump, never the push branch or a shared one; with no push branch set, that is also the
    default once the branch is on the remote. Inside a run, a worktree on another task's branch is
    refused (push.resolve). `--queue` lists the push queue
    (`delivery.push_queue`). Exit codes are in `push.py`."""
    from . import push
    if a.result:
        sys.exit(push.result(Path(a.result)))
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else next((c for d in [Path.cwd(), *Path.cwd().parents]
                                         if (c := Project(d)).exists()), None)
    if not p or not p.exists():
        die("ttp push: no tt-project project here (run it inside a run or a project's worktree)")
    if a.queue:
        from . import pushq
        print(pushq.queue_text(p))
        return
    if a.free:
        sys.exit(push.free(p, Path.cwd()))
    from . import worktree
    worktree.prepare(p, Path.cwd())   # a worktree a reviewer made itself gets the .venv its checks may run
    if a.marker:
        sys.exit(push.run_detached(p, Path.cwd(), Path(a.marker), a.own))
    if a.batch:
        from . import batch
        sys.exit(batch.run_batch(p, Path(a.batch)))
    sys.exit(push.detach(p, Path.cwd(), a.own) if a.detach else push.run(p, Path.cwd(), a.own))


CHECK_PASSES = "check_passes.json"   # under the project's state: {"passes": [{"tree", "commands", "prefixes", "run", "ts"}]}
CHECK_PASSES_MAX = 200                # the newest entries kept


_PLAIN_WORD = re.compile(r"[\w@%+=:,./ -]+")


def _command_key(c) -> str:
    """How a recorded pass names a check: a command line made only of plain words is compared as its
    words, so `pytest -q 'a.py'`, `pytest  -q a.py` and the argv `pytest -q a.py` are one command. Any
    shell syntax (quotes around `$`, globs, `;`, pipes, ...) can change what runs, so such a line, or a
    structured check, is compared as written."""
    if not isinstance(c, str):
        return str(c)
    try:
        words = shlex.split(c)
    except ValueError:
        return c
    if words and all(_PLAIN_WORD.fullmatch(w) for w in words):
        return shlex.join(words)
    return c


def _commands_hash(cmds: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([_command_key(c) for c in cmds]).encode()).hexdigest()


def _recorded_pass(p: Project | None, tree: str, cmds: list, purpose: str = "candidate") -> dict | None:
    """The recorded pass of exactly these commands, in this order, on this tree, for this purpose, or
    None. A pass of more commands that start with these is one of these too: they ran first, in order,
    and passed (the daemon's pre-started checks run the project's checks alone; a worker ran them with
    its own after `--`). A pass for the other purpose never counts (a record without one is candidate)."""
    if not p or not tree:
        return None
    try:
        passes = json.loads((p.state / CHECK_PASSES).read_text()).get("passes")
    except (OSError, ValueError, AttributeError):
        return None
    key = _commands_hash(cmds)
    for e in reversed(passes if isinstance(passes, list) else []):
        if isinstance(e, dict) and e.get("tree") == tree and e.get("purpose", "candidate") == purpose and (
                e.get("commands") == key or key in (e.get("prefixes") or ())):
            return e
    return None


def _record_pass(p: Project | None, tree: str, cmds: list, purpose: str = "candidate") -> None:
    """Only passes are recorded. A failed write is said and never fails the checks."""
    if not p or not tree:
        return
    import fcntl
    path = p.state / CHECK_PASSES
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)   # parallel runs of one project share the file
            try:
                passes = json.loads(path.read_text()).get("passes")
            except (OSError, ValueError, AttributeError):
                passes = None
            passes = [e for e in passes if isinstance(e, dict)] if isinstance(passes, list) else []
            passes.append({"tree": tree, "commands": _commands_hash(cmds),
                           "prefixes": [_commands_hash(cmds[:n]) for n in range(1, len(cmds))],
                           "run": os.environ.get("TTP_RUN_ID") or "?", "ts": time.time(),
                           **({"purpose": purpose} if purpose != "candidate" else {})})
            write_json(path, {"passes": passes[-CHECK_PASSES_MAX:]})
    except OSError as e:
        print(f"ttp checks: could not record the pass in {path}: {e}", file=sys.stderr)


CHECKS_ENV = "checks_env.json"   # under the project's state: the last check that failed at import, for doctor


def _note_checks_env(p: Project | None, cmd: str | None, problem: str | None, head: str,
                     source: str = "delivery.push_checks", also: bool = False) -> None:
    """Keep the last import-time failure of a check for `ttp doctor`; any other outcome clears it.
    `source` is where the command came from (delivery.push_checks, or `ttp checks --`); `also`, that
    tests failed in the same run as well, so the head's code is not cleared."""
    if not p:
        return
    path = p.state / CHECKS_ENV
    try:
        if problem:
            write_json(path, {"command": cmd, "problem": problem, "head": head, "source": source,
                              **({"also": True} if also else {}),
                              "run": os.environ.get("TTP_RUN_ID") or "?", "ts": time.time()})
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def checks_env_line(p: Project) -> str | None:
    """`ttp doctor`'s line for a check that last failed at import (CHECKS_ENV), or None."""
    try:
        e = json.loads((p.state / CHECKS_ENV).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(e, dict) or not e.get("problem"):
        return None
    when = timefmt.long(float(e.get("ts") or 0), p)
    what = ("tests failed, and also an environment problem" if e.get("also")
            else "an environment problem, not the code")
    return (f"{e.get('source') or 'delivery.push_checks'}: {e.get('command')!r} failed at import in run "
            f"{e.get('run') or '?'} at {when} ({what}): {e['problem']}")


def _extra_checks(extra: list[str], own: list) -> tuple[list[str], list[str]]:
    """(the commands given after `--`, the ones dropped). One word is a shell command line as
    written (`ttp checks -- 'FOO=1 pytest -q'`). Several words that each hold whitespace are one
    command each (`-- 'pytest -q tests/a.py' 'ruff check .'`); any other several words are argv,
    quoted so each stays one word (`-- pytest -k 'a and b'`). A command the same as one of the
    project's checks that runs on every head (no `if_exists`/`if_changed`), or as an earlier one
    here, is dropped: it would run twice. One that only a scoped check matches stays, as that
    check may be skipped on this head."""
    if not extra:
        return [], []
    if len(extra) == 1 or all(any(ch.isspace() for ch in w) for w in extra):
        given = [w for w in extra if w.strip()]
    else:
        given = [shlex.join(extra)]
    seen = {_command_key(c) for c in own if not (getattr(c, "if_exists", "") or getattr(c, "if_changed", ()))}
    mine, dropped = [], []
    for c in given:
        key = _command_key(c)
        (dropped if key in seen else mine).append(c)
        seen.add(key)
    return mine, dropped


def cmd_checks(a) -> None:
    """(inside a run) Run the local checks on this worktree's commit and record the result in the
    run's directory: the harness's gh opens a PR (even a draft) only after they passed on HEAD. The
    checks are the project's `delivery.push_checks`, plus the commands given after `--`.

    A pass is also recorded in the project's state, keyed on HEAD's tree and the ordered list of
    the commands that apply there. When the same commands, or more that start with them, already
    passed on the same tree (a reviewer checking a worker's commit, a rerun after a rebase that changed
    nothing), that pass is reused and nothing runs again; `--fresh` always runs them. Failures are never recorded, so a
    failure is never served as a pass. `ttp push` and the push queue never read this record and
    always run their own checks on the exact commit they push: a forged record can at most skip a
    local re-run, never let a change onto the branch. Where every project check is skipped on a commit,
    `ttp push --own` takes the commands given after `--` that this run recorded passing on exactly that
    commit and runs them itself before it pushes.

    `--detach` runs them in a session of their own that writes checks.rc in the run's directory
    however it ends, and prints a `--result` probe for `retry_when` that also answers once they were
    killed outright. Starting it again stops the run's earlier detached checks.

    `--purpose harness` qualifies a harness-preparation head: it runs the harness-tagged and untagged
    project checks plus the commands after `--`, lists candidate-tagged ones as not run, and fails
    unless a harness-tagged check applies to the head and ran or reused a recorded pass. Its pass is recorded as such and never counts for a PR,
    an on_pass hand-off or `ttp push --own`; its failures open no check gap."""
    from . import checkdirs, prguard, push
    if getattr(a, "gap", None):
        from . import checkgaps
        p = here()
        if not p:
            die("no project found for the check gap probe")
        sys.exit(0 if checkgaps.ready(p, a.gap, getattr(a, "gap_config", None)) else 1)
    if a.result:
        sys.exit(_checks_result(Path(a.result)))
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        die("ttp checks only works inside a tt-project run")
    if a.rc:
        sys.exit(_checks_child(a))
    base = os.environ.get("TTP_PROJECT")
    p = Project(base) if base else None
    cfg = p.config() if p and p.exists() else {}
    if not (p and p.exists()):
        p = None
    purpose = getattr(a, "purpose", None) or "candidate"
    own = push.check_list((cfg.get("delivery") or {}).get("push_checks"))
    not_run: list[str] = []
    if purpose == "harness":
        if bad := push.check_problems((cfg.get("delivery") or {}).get("push_checks")):
            die("ttp checks --purpose harness: delivery.push_checks is malformed, so nothing is qualified: "
                + "; ".join(bad), 1)
        if not any(c.purpose == "harness" for c in own):
            die("ttp checks --purpose harness: delivery.push_checks has no purpose harness check, so nothing "
                "qualifies this head; add a reviewed `ttp check-plan --purpose harness --definition-commit "
                "<full commit ID> --definition-path <path>` entry tagged \"purpose\": \"harness\"", 1)
        not_run = [f"not run: candidate publication gate: {c}" for c in own if c.purpose == "candidate"]
        own = [c for c in own if c.purpose != "candidate"]
    mine, dropped = _extra_checks(a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd, own)
    if dropped:
        print(f"ttp checks: not running {', '.join(map(repr, dropped))} after `--` again: "
              f"delivery.push_checks already runs it")
    cmds = own + mine
    if not cmds:
        die("ttp checks: the project sets no delivery.push_checks; give the repository's test commands after "
            "`--`, e.g. ttp checks -- pytest -q")
    # The checks run at the top of the worktree being checked, as `ttp push` runs them, wherever in
    # it this was started: a repo-relative check started in a subdirectory would test the wrong place.
    top = subprocess.run(["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    git = ["git", "-C", top or str(Path.cwd())]
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if not top or not head:
        die("ttp checks: run it in the change's git worktree")
    if p:
        from . import worktree
        worktree.prepare(p, top)   # a worktree a reviewer made itself gets the .venv its checks may run
    if subprocess.run([*git, "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                      text=True).stdout.strip():
        die("ttp checks: commit first; the checks are recorded for a commit, and this worktree has changes")
    tree = subprocess.run([*git, "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
    if a.detach:
        try:
            checkdirs.writable(run_dir)
            _checks_detach(a, Path(run_dir), p, top, head)
        except OSError as e:
            die(f"ttp checks: {e}")
        return
    try:
        checkdirs.writable(getattr(a, "check_run_dir", None) or run_dir)
    except OSError as e:
        die(f"ttp checks: {e}")
    log = Path(run_dir) / "checks.log"
    passed, failed, failure, rc = True, None, None, None
    base = _checks_base(p, Path(top), cmds)
    (Path(run_dir) / "check_failure.json").unlink(missing_ok=True)
    start = log.stat().st_size if log.exists() else 0
    with open(log, "a") as out:
        out.write(f"ttp checks: in {top} on {head[:12]}" + (" (purpose harness)" if purpose == "harness" else "")
                  + "\n")
        for line in not_run:
            print(f"ttp checks: {line}")
            out.write(f"{line}\n")
        try:
            todo, skipped = push.applicable(Path(top), head, cmds, lambda m: (print(f"ttp checks: {m}"),
                                                                               out.write(f"{m}\n")),
                                            base)
        except push.ScopeError as e:
            out.write(f"{e}\n")
            todo, skipped, passed, failed = [], [], False, str(e.check)
        # A harness run qualifies the head only through a harness check: one skipped by if_exists or
        # if_changed leaves the untagged checks and extras (or nothing) to pass, which is no qualification.
        unqualified = (purpose == "harness" and passed
                       and not any(getattr(c, "purpose", "") == "harness" for c in todo))
        if unqualified:
            failed = HARNESS_SKIPPED
            out.write(f"{failed}\n")
            passed, todo = False, []
        hit = _recorded_pass(p, tree, todo, purpose) if todo and not a.fresh else None
        if not todo and passed and push.outside_scope(cmds, skipped):
            out.write(f"{push.OUT_OF_SCOPE}\n")
            print(f"ttp checks: {push.OUT_OF_SCOPE}")
        elif not todo and passed:
            out.write(f"{push.NONE_APPLY}\n")
            passed, failed = False, push.NONE_APPLY
        elif hit:
            when = timefmt.long(float(hit.get("ts") or 0), p)
            said = (f"ttp checks: {len(todo)} check(s) passed on {tree[:12]} in run {hit.get('run') or '?'} "
                    f"at {when} (recorded); --fresh runs them again")
            out.write(f"{said}\n")
        for c in [] if hit else todo:
            out.write(f"$ {c}\n")
            out.flush()
            check_start = out.tell()
            # A detached run gives each check a group of its own, so a stop ends it with ttp checks.
            group = bool(getattr(a, "own_group", False))
            proc = subprocess.Popen(push.check_argv(c, top), cwd=top, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=group, env={**push.check_env("checks", run_dir=getattr(a, "check_run_dir", None)),
                                         "PWD": top},
                                    pass_fds=(a.tmp_fd,) if group and getattr(a, "tmp_fd", None) is not None else ())
            try:
                rc = proc.wait()
            except BaseException:
                if group:
                    _end_check(proc)
                raise
            if rc != 0:
                passed, failed = False, c
                break
    env_problem, also = None, False
    if not passed:
        with open(log, "rb") as f:   # this call's part of the log
            f.seek(start)
            text = f.read().decode("utf-8", errors="replace")
        env_problem = push.env_problem(text, top) if failed in todo else None
        also = bool(env_problem) and push.test_failures(text)
        if failed in todo and rc is not None and purpose == "candidate":   # a harness run opens no check gap
            with open(log, "rb") as f:
                f.seek(check_start)
                output = f.read().decode("utf-8", errors="replace")
            failure = push.applicability_failure(Path(top), head, push.Check(failed) if not isinstance(failed, push.Check)
                                          else failed, rc, output, base)
            if failure:
                write_json(Path(run_dir) / "check_failure.json", failure)
    write_json(Path(run_dir) / prguard.CHECKS_FILE, {"head": head, "worktree": top, "passed": passed,
                                                     "commands": todo, "skipped": skipped, "ts": time.time(),
                                                     "purpose": purpose, **({"not_run": not_run} if not_run else {}),
                                                     **({"env_problem": env_problem} if env_problem else {})})
    source = "delivery.push_checks" if any(str(c) == str(failed) for c in own) else "ttp checks --"
    _note_checks_env(p, str(failed) if env_problem else None, env_problem, head, source, also)
    if not passed:
        from . import trim
        if unqualified:
            die(f"ttp checks --purpose harness: {HARNESS_SKIPPED} on {head[:12]}, so nothing qualifies this "
                f"head; the purpose harness check must apply to it (full output: {log})", 1)
        print(trim.summary(text))   # a test run's failures, else head and tail
        if also:
            die(f"ttp checks: {failed!r} failed on {head[:12]}: tests failed, and there is also an environment "
                f"problem: {env_problem} (full output: {log})", 1)
        if env_problem:
            die(f"ttp checks: {failed!r} failed at import on {head[:12]}: an environment problem, not the "
                f"head's code: {env_problem} (full output: {log})", 1)
        die(f"ttp checks: {failed!r} failed on {head[:12]} (full output: {log})", 1)
    if hit:
        print(said)
        return
    _record_pass(p, tree, todo, purpose)
    more = f", {len(skipped)} skipped as not applicable" if skipped else ""
    more += f", {len(not_run)} candidate gate(s) not run" if not_run else ""
    print(f"ttp checks: {len(todo)} check(s) passed on {head[:12]}{more}; "
          + ("recorded as a harness qualification only: it never counts for a PR, an on_pass hand-off or a push"
             if purpose == "harness" else "recorded for the draft PR"))


HARNESS_SKIPPED = "every purpose harness check was skipped as not applicable"


def _harness_purpose(a) -> dict:
    """What a detached or relaunched `ttp checks` keeps of `--purpose`: only harness, so a restart
    after a reboot qualifies the head for the same purpose."""
    return {"purpose": "harness"} if getattr(a, "purpose", None) == "harness" else {}


def _checks_base(p: Project | None, top: Path, cmds: list) -> str:
    """What `ttp checks` compares `if_changed` scopes with: the push target as last fetched, or ""
    (no project, no push target or no copy of it here), and then those checks run."""
    from . import push
    if not p:
        return ""
    try:
        remote, branch = push.target(p, top)
    except ValueError:
        return ""
    r = subprocess.run(["git", "-C", str(top), "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


CHECKS_RC, CHECKS_PID, CHECKS_OUT = "checks.rc", "checks.pid", "checks.out"   # in the run's directory

CHECKS_TMP, CHECKS_TMP_LOCK = "checks-tmp", "checks-tmp.lock"


def _checks_tmp_cleanup(run_dir: Path) -> None:
    """Remove abandoned scratch only while its lifetime lock is free. The launcher, checks
    process and check commands inherit the same lock, so recovery never removes live storage,
    even between launch and checks.pid or after the checks supervisor was killed."""
    from . import checkdirs
    try:
        with checkdirs.directory(run_dir):
            info = _read_checks_pid(run_dir)
        base = os.environ.get("TTP_PROJECT")
        output = info.get("check_run_dir")
        if base and output and Path(output).parent == Project(base).state / checkdirs.FOLDER:
            checkdirs.cleanup(Path(output), whole=True)
    except OSError:
        pass


def _end_check(proc: subprocess.Popen) -> None:
    """End a check run in its own group: TERM, then KILL if it is still there after 5 s."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            pass


def _checks_alive(info: dict) -> bool:
    """The detached checks recorded in `info` still run: same pid and start, not a zombie."""
    from .runner import proc_start
    pid, started = info.get("pid"), info.get("started")
    return isinstance(pid, int) and bool(started) and proc_start(pid) == started and not zombie(pid)


def _checks_elsewhere(info: dict) -> bool:
    """This process cannot judge the recorded checks' pid: it runs boxed (push._boxed), or in a PID
    namespace other than the one that started them."""
    from . import push
    ns = push.pid_ns()
    return push._boxed() or bool(info.get("pidns") and ns and ns != info["pidns"])


def _read_checks_pid(run_dir: Path) -> dict:
    try:
        info = json.loads((run_dir / CHECKS_PID).read_text())
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def _checks_detach(a, run_dir: Path, p: Project | None, top: str = "", head: str = "") -> None:
    """Start `ttp checks` in a session of its own. It writes checks.rc however it ends (a stop by
    TERM, HUP or INT included); the printed `--result` probe also wakes the task when it was killed
    outright. This run's earlier detached checks are stopped first, so nobody needs `pkill`.
    A tool sandbox queues the launch and cancellation through the daemon (detach.tend).
    checks.pid also keeps the worktree, head and command, so the daemon can start the same checks
    again, model-free, after a reboot killed them (Daemon._relaunch_checks)."""
    from . import detach, push
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    rc = run_dir / CHECKS_RC
    detach.lost(rc).unlink(missing_ok=True)
    # A request the daemon has not finished starting is replaced through the queue too, so the daemon
    # never starts an older request after this one.
    if push._boxed() or (p and detach.pending(rc)):
        if not p:
            die("ttp checks: sandboxed detached checks need a project daemon; set TTP_PROJECT")
        detach.enqueue(p.state, rc, run_dir / CHECKS_OUT, [], top or os.getcwd(), env,
                       checks={"worktree": top, "head": head, "cmd": list(a.cmd), "fresh": bool(a.fresh),
                               **_harness_purpose(a)})
        print("ttp checks: queued; the daemon starts them outside this command's sandbox")
    else:
        detach.withdraw(rc)   # a marker left without a request (no daemon to take it)
        pid = _checks_start(a, run_dir, top, head, env)
        print(f"ttp checks: started in the background (pid {pid}); output in {run_dir / CHECKS_OUT}, "
              f"exit code in {rc}")
    print(f"retry_when: {push._own_ttp(p)} checks --result {shlex.quote(str(run_dir))}")
    print("on_pass: if nothing is left after them but recording the result, put that final hand-off "
          "(done or needs_review) in the waiting hand-off's on_pass; a pass records it without a model run")


def _checks_start(a, run_dir: Path, top: str, head: str, env: dict, req: str = "") -> int:
    """Launch outside the tool sandbox, cancelling the earlier host process before replacing its
    metadata. The child runs the checks only on this launcher's `go`, sent once checks.pid durably
    names it (or, if the launcher died after that write, once it finds itself named there): a
    launcher that dies before the write leaves a child that starts nothing. `req` is the queued
    request this launch serves (detach.tend)."""
    from .push import pid_ns
    from .runner import boot_id, proc_start
    from . import checkdirs
    checkdirs.writable(str(run_dir))
    old = _read_checks_pid(run_dir)
    if _checks_alive(old):
        try:
            os.killpg(old["pid"], signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 15
        while _checks_alive(old) and time.time() < deadline:
            time.sleep(0.1)
        if _checks_alive(old):
            try:
                os.killpg(old["pid"], signal.SIGKILL)
            except OSError:
                pass
    base = env.get("TTP_PROJECT")
    if not base:
        raise OSError("TTP_RUN_DIR for detached checks requires a project state directory (TTP_PROJECT)")
    # Refuse a linked legacy lock, and preserve the old commands' lifetime on upgrade.
    previous = Path(old["check_run_dir"]) if old.get("check_run_dir") else run_dir
    if previous != run_dir and previous.parent != Project(base).state / checkdirs.FOLDER:
        raise OSError("TTP_RUN_DIR in checks.pid is outside the project's check output directory")
    old_guard = checkdirs.take(previous) if previous.exists() or previous.is_symlink() else False
    if old_guard is None:
        raise OSError("earlier check commands still own their temporary storage; not starting replacement checks")
    if old_guard:
        old_guard.close()
        if previous != run_dir:
            checkdirs.finish(previous)
    output = checkdirs.new(Project(base).state, run_dir / CHECKS_PID)
    guard = checkdirs.take(output)
    launched = False
    try:
        env = checkdirs.environment(output, env)
        # The supervisor records checks.log/rc in the original run; commands get the report dir.
        env['TTP_RUN_DIR'] = str(run_dir)
        env['TTP_CHECKS_TMP_FD'] = str(guard.fileno())
        (run_dir / CHECKS_RC).unlink(missing_ok=True)
        with open(run_dir / CHECKS_OUT, "wb") as out:
            child = subprocess.Popen([sys.executable, "-m", "ttp", "checks", "--rc", str(run_dir / CHECKS_RC),
                                      *(["--fresh"] if a.fresh else []),
                                      *(["--purpose", a.purpose] if getattr(a, "purpose", None) else []), *a.cmd],
                                     cwd=top or Path.cwd(), env=env, stdin=subprocess.PIPE, stdout=out,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(guard.fileno(),))
        try:
            write_json(run_dir / CHECKS_PID, {"pid": child.pid, "started": proc_start(child.pid), "ts": time.time(),
                                              "boot": boot_id(), "worktree": top, "head": head, "cmd": list(a.cmd),
                                              "fresh": bool(a.fresh), **_harness_purpose(a), "req": req, "pidns": pid_ns(),
                                              "check_run_dir": str(output), "check_report_dir": str(checkdirs.reports(output))})
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)   # this launch's own child, still waiting for its go
            except OSError:
                pass
            child.wait()
            raise
        try:
            child.stdin.write(b"go\n")
            child.stdin.close()
        except OSError:
            pass   # it died: the probe reports that
        launched = True
        return child.pid
    finally:
        guard.close()
        if launched:
            checkdirs.cleanup(output, whole=True)
        else:
            checkdirs.finish(output)


def _checks_released(go: bytes, run_dir: Path) -> bool:
    """The detached child may run: its launcher sent `go`, or died after checks.pid named this
    very process (pid and start), so its identity is durable either way."""
    from .runner import proc_start
    if go.startswith(b"go"):
        return True
    info = _read_checks_pid(run_dir)
    return info.get("pid") == os.getpid() and bool(info.get("started")) and info.get("started") == proc_start(os.getpid())


def _checks_child(a) -> int:
    """The detached `ttp checks`: run them, then write the exit code to `--rc` however it ends."""
    rc_file = Path(a.rc)
    tmp_fd = int(os.environ["TTP_CHECKS_TMP_FD"]) if "TTP_CHECKS_TMP_FD" in os.environ else None
    released = False
    code: int | str = 1

    def stop(sig, _frame):
        raise SystemExit(128 + sig)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        released = _checks_released(sys.stdin.buffer.read(), rc_file.parent)
        if not released:
            # A newer launch may own checks.rc: this unrecorded child leaves it alone.
            print("ttp checks: not started: the launcher ended before it recorded this process", flush=True)
            return 1
        from . import checkdirs
        output = checkdirs.writable(os.environ.get("TTP_CHECK_OUTPUT_DIR"))
        cmd_checks(argparse.Namespace(**{**vars(a), "rc": None, "detach": False, "own_group": True,
                                         "check_run_dir": output, "tmp_fd": tmp_fd}))
        code = 0
    except OSError as e:
        print(f"ttp checks: {e}", file=sys.stderr, flush=True)
        code = 1
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code > 128:
            with open(rc_file.parent / "checks.log", "a") as log:
                log.write(f"ttp checks: stopped by signal {code - 128}\n")
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
            from . import checkdirs
            info = _read_checks_pid(rc_file.parent)
            if info.get('pid') == os.getpid() and info.get('check_run_dir'):
                checkdirs.finish(Path(info['check_run_dir']))
            _checks_tmp_cleanup(rc_file.parent)
        if released:
            durable_write(rc_file, f"{code}\n")
    return code if isinstance(code, int) else 1


def _checks_result(run_dir: Path) -> int:
    """`retry_when` probe of detached checks: 0 once checks.rc exists or the checks died without
    writing it (it then writes `killed` there), 1 while they run."""
    rc_file = run_dir / CHECKS_RC
    from . import detach
    _checks_tmp_cleanup(run_dir)
    if detach.pending(rc_file):
        print("ttp checks: queued for the daemon, which starts them outside the sandbox")
        return 1
    if detach.lost(rc_file).exists():
        durable_write(rc_file, "killed\n")
        print(f"ttp checks: not started: {detach.lost(rc_file).read_text().strip()}")
        return 0
    info = _read_checks_pid(run_dir)
    if info and not rc_file.exists() and _checks_elsewhere(info):
        # Its pid means nothing in this PID namespace: never call it dead (nor write `killed`) from
        # here. The daemon's own probe of retry_when sees the host's processes and settles it.
        print(f"ttp checks: started outside this sandbox (pid {info.get('pid')}); running unless the daemon's "
              "probe finds otherwise")
        return 1
    alive = _checks_alive(info)
    if not rc_file.exists() and alive:
        print(f"ttp checks: running (pid {info['pid']})")
        return 1
    if rc_file.exists():
        print(f"ttp checks: finished with exit code {rc_file.read_text().strip()}")
    elif not info:
        print(f"ttp checks: no detached checks recorded in {run_dir}")
        return 0
    else:
        durable_write(rc_file, "killed\n")
        print(f"ttp checks: the detached checks (pid {info.get('pid')}) ended without an exit code: something "
              "killed them; run `ttp checks --detach` again")
    try:
        tail = (run_dir / CHECKS_OUT).read_text(errors="replace").splitlines()[-20:]
    except OSError:
        tail = []
    if tail:
        print("\n".join(tail))
    return 0


def cmd_clip(a) -> None:
    """Run one command with its whole output in a file, and print only what a model needs: a test
    run's failures, else the head and tail, plus the file's path. Exits with the command's code.
    Inside a run the file goes in the run's folder (out/), else in a temporary one."""
    from . import trim
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        die("usage: ttp clip -- <command>")
    run_dir = os.environ.get("TTP_RUN_DIR")
    folder = Path(run_dir) / "out" if run_dir else Path(tempfile.mkdtemp(prefix="ttp-clip-"))
    folder.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for _ in folder.glob("clip-*.log"))
    log = folder / f"clip-{n}.log"
    with open(log, "w") as out:
        rc = subprocess.run(shlex.join(cmd) if len(cmd) > 1 else cmd[0], shell=True, stdout=out,
                            stderr=subprocess.STDOUT).returncode
    text = log.read_text(errors="replace")
    print(trim.summary(text, str(log), a.lines))
    print(f"(exit {rc})")
    sys.exit(rc)


def cmd_killscan(a) -> None:
    """Check scripts for kills by name or pattern (`pkill`, `killall`, `pgrep`/`pidof`, `ps | grep`) before
    running them: such a kill can match the worker's own tool shell. Exits 1 when one is found.
    `--shim <dir>` writes logging stand-ins for those commands; put <dir> first on PATH to run the
    script without them killing anything."""
    from . import killscan
    if a.shim:
        folder = Path(a.shim).resolve()
        killscan.write_shims(folder)
        print(f"shims for {', '.join(killscan.SHIMMED)} in {folder}; calls are logged to "
              f"{folder / 'killscan.log'}. Run the script with: PATH={shlex.quote(str(folder))}:\"$PATH\" <script>")
    hits = 0
    for name in a.files:
        try:
            text = Path(name).read_text(errors="replace")
        except OSError as e:
            die(f"ttp killscan: cannot read {name}: {e.strerror or e}")
        for n, reason, line in killscan.scan(text):
            hits += 1
            print(f"{name}:{n}: {reason}: {line}")
    if a.files:
        print(f"ttp killscan: {hits} kill(s) by name or pattern found" if hits else
              "ttp killscan: no kills by name or pattern found")
    elif not a.shim:
        die("usage: ttp killscan <script>... [--shim <dir>]")
    sys.exit(1 if hits else 0)


def cmd_lock(a) -> None:
    """Hold one slot of a shared resource while a command runs: `ttp lock <resource> -- <cmd...>`.

    Parallel tasks share a device or a remote build directory this way: each takes the lock only for
    the commands that touch it, and the rest of the task runs alongside other work. Slots come from
    the project's `resources` config (default 1); a running `exclusive:<resource>` task holds one for
    its whole run, and one waiting for a slot reserves the resource: new commands wait until it has
    started. The lock is held until the command has ended, however it ends. Every name in config
    `device.locks` is the one device lock. Waiters are served in arrival order, and a nested
    `ttp lock` of a lock this command already holds just runs.

    Inside a run, waiting is reported in the run's progress (a wait is not a stall), does not count
    toward the run's wall clock (which grows by at most its own length this way), and gives up after
    half the run's stall limit (a longer --timeout is capped to that; 0 means that limit too). Giving
    up exits 75: the task hands back `waiting` with retry_when `ttp lock --probe <resource>`. A paused
    resource exits 75 as well. In a `ttp detach` job, or a background (`nohup ... &`) driver that no longer
    runs under the run's agent, there is no cap.
    `ttp lock --probe <resource>` exits 0 when the resource is free, not paused, not reserved and
    nobody queues for it, else 75.
    """
    from . import locks as lk
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd and not a.probe:
        die("usage: ttp lock <resource> -- <command...>  |  ttp lock --probe <resource>")
    base = os.environ.get("TTP_PROJECT")
    if not base and a.probe:
        base = next((str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
    if not base:
        die("ttp lock only works inside a tt-project run (or with TTP_PROJECT set)")
    p = Project(base)

    def _pause() -> dict | None:
        try:
            paused = p.db.paused_resources()
            # A pause of any of the device's names holds them all.
            return paused.get(a.resource) or next((v for k, v in paused.items() if lk.canonical(cfg, k) == res), None)
        except Exception as e:   # an unreadable database must not stop device commands
            print(f"ttp lock: could not check for a pause of {a.resource}: {e}", file=sys.stderr, flush=True)
            return None

    def _refuse_paused() -> None:
        # Checked before each try, so a pause set while this waits holds too.
        held = _pause()
        if held is not None:
            if waiting:
                _end_wait()
            why = f" ({held['reason']})" if held.get("reason") else ""
            die(f"{a.resource} is paused{why}; hand the task back as waiting until it is resumed", 75)

    from . import shared
    cfg = p.config()
    res = lk.canonical(cfg, a.resource)
    where = shared.locks_dir(p, res, cfg)

    def _unwritable(e: OSError) -> None:
        # Never a private lock instead: other holders would not see it. Say what is wrong.
        die(f"cannot take the lock of {a.resource}: {where} is not writable here ({e.strerror or e}). "
            f"The run's sandbox or file permissions must allow writing it; hand the task back as blocked "
            f"and name this directory")

    try:
        paths = lk.slot_paths(where, res, shared.slots(p, res, cfg))
    except OSError as e:
        _unwritable(e)
    if a.probe:
        if (held := _pause()) is not None:   # a paused `ttp lock` exits 75, so its probe must too
            print(f"{a.resource}: paused" + (f" ({held['reason']})" if held.get("reason") else ""))
            sys.exit(75)
        free = lk.probe(where, res, paths)
        n, who_r = len(lk.queued(where, res)), lk.reserved_by(lk.reserve_path(where, res))
        print(f"{a.resource}: " + ("free" if free else f"busy (held by {', '.join(lk.holders(paths, p)) or 'nobody'}"
                                                      f"; {n} waiting{f'; reserved for {who_r}' if who_r else ''})"))
        sys.exit(0 if free else 75)
    who = shared.holder(p, res, f"task #{os.environ.get('TTP_TASK') or '?'} "
                                f"(run {os.environ.get('TTP_RUN_ID') or '?'})", cfg)
    held_locks = [x for x in os.environ.get("TTP_LOCKS_HELD", "").split(",") if x]
    if res in held_locks:
        sys.exit(subprocess.call(cmd))   # an enclosing `ttp lock` of this command holds it already
    child_env = {**os.environ, "TTP_LOCKS_HELD": ",".join(held_locks + [res])}
    if res in lk.device_locks(cfg) or res.endswith("-device") or a.resource.endswith("-device"):
        child_env["TTP_NO_DEVICE"] = "0"   # a device lock is the sanctioned way to a device
        child_env = unmask_devices(child_env)   # and sees the devices a run's own commands do not
    detached = bool(os.environ.get("TTP_DETACHED"))
    run_dir = Path(os.environ["TTP_RUN_DIR"]) if os.environ.get("TTP_RUN_DIR") else None
    try:
        spec = json.loads((run_dir / "run.json").read_text()) if run_dir else {}
    except (OSError, ValueError):
        spec = {}
    waiting = False
    if res in {lk.canonical(cfg, x.get("resource") or "") for x in spec.get("exclusive") or []}:
        # This run's task holds the resource for its whole run already.
        _refuse_paused()
        sys.exit(subprocess.call(cmd, env=child_env))
    timeout = a.timeout
    cap = float(spec.get("stall_s") or 0) / 2
    capped = False
    if cap and not detached:
        if timeout is not None and (timeout == 0 or timeout > cap):
            print(f"ttp lock: --timeout {timeout:.0f} capped to {cap:.0f} s inside a run; if it runs out, "
                  f"hand off `waiting` with retry_when `ttp lock --probe {a.resource}`", file=sys.stderr, flush=True)
        capped = not timeout or timeout > cap
        timeout = cap if timeout is None or timeout == 0 else min(timeout, cap)
    mark = lk.reserve_path(where, res)
    wait_dir = None if detached else run_dir
    started, told = time.time(), 0.0
    wait_key = f"{os.getpid()}:{started}"

    def _end_wait(*_):
        # Cleared first: a signal arriving while this records would otherwise take the record's
        # flock a second time in this process and hang.
        nonlocal waiting
        if waiting:
            waiting = False
            lk.record_wait(wait_dir, wait_key, started, time.time())

    try:
        ticket = lk.enqueue(where, res, who)
    except OSError as e:
        _unwritable(e)
    atexit.register(lambda: lk.dequeue(ticket))   # however this ends; a dead process's ticket is dropped too
    while True:
        _refuse_paused()
        reserved = lk.reserved_by(mark)
        try:
            f = None if reserved else lk.take_in_turn(where, res, ticket, paths, who, " ".join(cmd))
        except OSError as e:
            _end_wait()
            _unwritable(e)
        if f:
            _end_wait()
            lk.dequeue(ticket)
            ticket = None
            waited = time.time() - started
            if waited > 5:
                print(f"ttp lock: got {a.resource} after {waited / 60:.1f} min", file=sys.stderr, flush=True)
            # The lock is held until the command itself has ended. A signal to this process (a
            # timeout, a cancel) is passed on to the command, and the lock is released only once
            # the command is gone, so nobody else ever gets the resource while it is still in use.
            proc = subprocess.Popen(cmd, env=child_env)

            def _forward(signum, _frame):
                try:
                    proc.send_signal(signum)
                except OSError:
                    pass

            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, _forward)
            try:
                rc = proc.wait()
            finally:
                f.close()
            sys.exit(rc)
        if wait_dir and not waiting:
            # The run's wall clock stops while it waits here; the supervisor reads this record.
            # A signal ends the wait through _end_wait, so the record never stays open.
            lk.record_wait(wait_dir, wait_key, started, None)
            waiting = True
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda signum, _f: (_end_wait(), sys.exit(128 + signum)))
        if capped and timeout and time.time() - started > timeout and not lk.in_run(run_dir):
            # A driver started in the background (`nohup ... &`) outlives the run: the run's cap is not its own,
            # and its wait is no longer the run's.
            capped, timeout = False, a.timeout
            _end_wait()
            wait_dir = None
            print(f"ttp lock: not under the run's agent; waiting for {a.resource} without the run's cap",
                  file=sys.stderr, flush=True)
        if timeout and time.time() - started > timeout:
            _end_wait()
            die(f"{a.resource} stayed busy for {timeout:.0f} s. Hand the task back now: result.json status "
                f"`waiting`, waiting_for `{a.resource}`, retry_when `ttp lock --probe {a.resource}`", 75)
        if time.time() - told >= 120:
            ahead = len(lk.queued(where, res)) - 1
            line = (f"waiting for {a.resource} (reserved for {reserved})" if reserved else
                    f"waiting for {a.resource} (held by {', '.join(lk.holders(paths, p)) or 'another task'}"
                    f"{f'; {ahead} ahead in queue' if ahead > 0 else ''})")
            print(f"ttp lock: {line}", file=sys.stderr, flush=True)
            if run_dir:
                try:
                    with open(run_dir / "progress.md", "a") as pf:
                        pf.write(f"{time.strftime('%H:%M:%S')} {line}\n")
                except OSError:
                    pass
            told = time.time()
        time.sleep(poll_s(3))

def cmd_landed(a) -> None:
    """Probe for start_when/retry_when: exit 0 once a commit, or a task's landing (`--task <id>`),
    is on the push branch, also as rebased commits; 1 while not yet; 2 when it cannot tell. The
    daemon runs a `landed:#<id>` probe as `ttp landed --task <id>` (see landed.py)."""
    from . import landed
    p = here()
    if not p:
        print("ttp landed: no project here (run it in a project or with TTP_PROJECT set)")
        sys.exit(landed.CANNOT)
    sys.exit(landed.run(p, a))


def cmd_detach(a) -> None:
    """Start a long job that outlives this run: `ttp detach <name> -- <command...>`.

    Output goes to $TTP_RUN_DIR/<name>.log and the exit code to $TTP_RUN_DIR/<name>.rc once it
    ends. Hand off `waiting` with the retry_when it prints, `ttp detach --check <rc path>`: it exits 0
    once the job wrote its .rc, or once its process is gone without one (a kill, a reboot: it says
    why, see detach.state), else 1. From a command a sandbox would end with everything it started,
    the daemon starts the job (detach.py).
    A run that ends without a hand-off after a detach is also brought back as waiting on its jobs. The
    resources an `exclusive:` task holds for its whole run stay held until its jobs end (hold.py)."""
    from . import detach, jobuse
    from . import locks as lk
    from .push import _boxed, _own_ttp
    from .runner import boot_id
    if a.check is not None:   # `--check --host <alias> <path>` leaves the paths to the positionals
        a.check = [*a.check, *([a.name] if a.name else []), *(a.command or [])]
        if not a.check:
            die("usage: ttp detach --check <rc path...>  |  ttp detach --check --host <ssh alias> <dir>/<name>...")
        if a.host:
            sys.exit(_detach_check_remote(a.host, a.check))
    if a.remote:
        _detach_remote(a, _own_ttp())
        return
    if a.check is not None:
        ended = True
        for rc in map(Path, a.check):
            st, why = detach.state(rc)
            if st == "ended":
                print(f"{rc.stem}: ended, exit {why}")
            elif st == "lost":
                print(f"{rc.stem}: gone without an exit code ({why}); see {rc.with_suffix('.log')}")
            else:
                ended = False
                print(f"{rc.stem}: {'queued for the daemon, which starts it outside the sandbox' if st == 'queued' else 'running'}")
        sys.exit(0 if ended else 1)
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not cmd or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.name or ""):
        die("usage: ttp detach <name> -- <command...>  |  ttp detach --check <rc path...>   "
            "(name: letters, digits, . _ -)")
    if not run_dir:
        die("ttp detach only works inside a tt-project run")
    rd = Path(run_dir).resolve()
    rc, logf = rd / f"{a.name}.rc", rd / f"{a.name}.log"
    if any(f.exists() for f in (rc, logf, detach.queued(rc), detach.lost(rc))):
        die(f"a detached job named {a.name} already ran in this run; pick another name")
    pid = None
    if _boxed() and rd.parent.name == "runs":   # this command's sandbox would end the job: the daemon starts it
        detach.enqueue(rd.parent.parent, rc, logf, cmd, os.getcwd(), dict(os.environ))
    else:
        # The job inherits this lock and holds it while any of its processes lives, so a job gone
        # without an .rc is told apart from one still running (detach.state).
        lock = lk.try_take([lk.job_lock(rc)], f"detached job {a.name}", " ".join(cmd))
        if lock is None:
            die(f"a detached job named {a.name} is still running in this run; pick another name")
        try:
            pid = detach.start(rc, logf, cmd, os.getcwd(), dict(os.environ), lock)
        finally:
            lock.close()
    reg = rd / "detached.json"
    try:
        jobs = json.loads(reg.read_text())
    except (OSError, ValueError):
        jobs = []
    jobs = jobs if isinstance(jobs, list) else []
    jobs.append({"name": a.name, "rc": str(rc), "log": str(logf), "pid": pid, "started": time.time(),
                 "boot": boot_id(), "queued": pid is None, "command": " ".join(cmd)[:300],
                 "requested": jobuse.requested(cmd, os.environ, getattr(a, "cores", None),
                                               getattr(a, "devices", None), getattr(a, "mem_gb", None),
                                               jobuse.daemon_env(rd.parent.parent / "daemon.pid")
                                               if rd.parent.name == "runs" else None)})
    durable_write(reg, json.dumps(jobs, indent=1))
    how = (f"(pid {pid})" if pid else
           "(queued: this command's sandbox would end it, so the daemon starts it within seconds)")
    print(f"detached {a.name} {how}\nlog: {logf}\nrc:  {rc}\n"
          f"hand off: status waiting, retry_when \"{lk.job_probe([rc], _own_ttp())}\"")


# The remote side of `ttp detach --remote`, run by POSIX sh over ssh. The driver's wrapper records its
# own pid and its process start (field 22 of /proc/<pid>/stat; without /proc, as on macOS, ps's lstart),
# next to the host's boot_id (else kern.bootsessionuuid, else kern.boottime's seconds, which moves when the
# clock is stepped): a pid alive with another start, or a boot that changed, is a driver that is gone even
# with no .rc. A .boot holding kern.boottime's seconds, as older versions wrote, still names its boot.
_REMOTE_START = r'''d=$1 n=$2; shift 2
case $d in "~"|"~/"*) d=$HOME${d#\~} ;; esac
mkdir -p "$d" || exit 3
p=$d/$n
if [ -e "$p.log" ] || [ -e "$p.rc" ] || [ -e "$p.pid" ]; then echo "exists $p"; exit 4; fi
b=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
[ -n "$b" ] || b=$(sysctl -n kern.bootsessionuuid 2>/dev/null)
[ -n "$b" ] || b=$(sysctl -n kern.boottime 2>/dev/null | sed -n "s/^{ sec = \([0-9]*\),.*/\1/p")
if [ -n "$b" ]; then echo "$b"; fi >"$p.boot"
setsid nohup sh -c 'p=$1; shift
s=$(sed "s/.*) //" /proc/$$/stat 2>/dev/null | cut -d" " -f20)
[ -n "$s" ] || s=$(ps -o lstart= -p $$ 2>/dev/null | tr -s " " _)
echo "$(date +%s) $s" >"$p.start"
echo $$ >"$p.pid.tmp"; mv "$p.pid.tmp" "$p.pid"
"$@" >"$p.log" 2>&1 </dev/null; c=$?; echo $c >"$p.rc.tmp"; mv "$p.rc.tmp" "$p.rc"' sh "$p" "$@" \
  >/dev/null 2>&1 </dev/null &
i=0; while [ ! -s "$p.pid" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
[ -s "$p.pid" ] || { echo "nopid $p"; exit 5; }
echo "started $(cat "$p.pid") $p"
'''

_REMOTE_CHECK = r'''p=$1
case $p in "~"|"~/"*) p=$HOME${p#\~} ;; esac
p=${p%.rc}
if [ -e "$p.rc" ]; then echo "rc $(cat "$p.rc")"; exit 0; fi
if [ ! -s "$p.pid" ]; then echo "none $p"; exit 0; fi
if [ -s "$p.boot" ]; then
  r=$(cat "$p.boot") s=
  b=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
  if [ -z "$b" ]; then
    b=$(sysctl -n kern.bootsessionuuid 2>/dev/null)
    s=$(sysctl -n kern.boottime 2>/dev/null | sed -n "s/^{ sec = \([0-9]*\),.*/\1/p")
  fi
  if [ "$r" != "${b:-$s}" ] && [ "$r" != "$s" ]; then echo "boot"; exit 0; fi
fi
pid=$(cat "$p.pid")
if kill -0 "$pid" 2>/dev/null; then
  st=$(sed "s/.*) //" /proc/$pid/stat 2>/dev/null | cut -d" " -f20)
  [ -n "$st" ] || st=$(ps -o lstart= -p "$pid" 2>/dev/null | tr -s " " _)
  want=$(cut -d" " -f2 "$p.start" 2>/dev/null)
  if [ -z "$st" ] || [ -z "$want" ] || [ "$st" = "$want" ]; then echo "running $pid"; exit 0; fi
fi
if [ -e "$p.rc" ]; then echo "rc $(cat "$p.rc")"; exit 0; fi
echo "gone"
'''


def _ssh_sh(host: str, script: str, args: list, timeout: float = 60) -> subprocess.CompletedProcess | None:
    """Run `script` with sh on `host` over ssh, with `args` as its $1...; None when ssh cannot run."""
    remote = "sh -c " + " ".join(shlex.quote(x) for x in (script, "sh", *map(str, args)))
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, remote],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _detach_remote(a, ttp: str) -> None:
    """`ttp detach --remote <ssh alias> [--dir <remote dir>] <name> -- <command...>`: start the driver
    on that host with setsid nohup, so it outlives this run, ssh and a reboot of this host. <name>.log,
    .rc, .pid, .boot and .start go to the remote folder (default ~/.ttp-detach/<run>); the retry_when
    it prints, `ttp detach --check --host <alias> <dir>/<name>`, exits 0 once the .rc exists or the
    driver is gone (pid dead, or the host restarted), 1 while it runs and 255 when ssh fails."""
    cmd = list(a.command or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.name or "") or a.remote.startswith("-"):
        die("usage: ttp detach --remote <ssh alias> [--dir <remote dir>] <name> -- <command...>   "
            "(name: letters, digits, . _ -)")
    run = Path(os.environ.get("TTP_RUN_DIR") or "").name or time.strftime("manual-%Y%m%d-%H%M%S")
    rdir = (a.dir or f"~/.ttp-detach/{run}").rstrip("/") or "/"
    r = _ssh_sh(a.remote, _REMOTE_START, [rdir, a.name, *cmd])
    if r is None or r.returncode == 255:
        die(f"ssh to {a.remote} failed{': ' + r.stderr.strip()[-300:] if r and r.stderr.strip() else ''}", 255)
    out = (r.stdout.strip().splitlines() or [""])[-1].split(" ", 2)
    if r.returncode == 4:
        die(f"a detached job named {a.name} already ran in {rdir} on {a.remote}; pick another name")
    if r.returncode or out[0] != "started" or len(out) < 3:
        die(f"could not start {a.name} on {a.remote} (exit {r.returncode}): "
            f"{(r.stdout + r.stderr).strip()[-300:]}")
    pid, prefix = out[1], out[2]
    print(f"detached {a.name} on {a.remote} (pid {pid})\nlog: {prefix}.log\nrc:  {prefix}.rc\n"
          f"hand off: status waiting, survives_reboot true, retry_when "
          f"\"{ttp} detach --check --host {shlex.quote(a.remote)} {shlex.quote(prefix)}\"")


def _detach_check_remote(host: str, prefixes: list) -> int:
    """`ttp detach --check --host <alias> <dir>/<name>...`: 0 once every job ended or is gone, 1 while
    any runs, 255 when ssh fails (retry_when keeps the task asleep), 2 on an answer it cannot read."""
    ended = True
    for pre in prefixes:
        name = Path(pre[:-3] if pre.endswith(".rc") else pre).name
        r = _ssh_sh(host, _REMOTE_CHECK, [pre])
        if r is None or r.returncode == 255:
            print(f"{name}: ssh to {host} failed{': ' + r.stderr.strip()[-200:] if r and r.stderr.strip() else ''}")
            return 255
        out = (r.stdout.strip().splitlines() or [""])[-1].split(" ", 1)
        if r.returncode or out[0] not in ("rc", "none", "boot", "running", "gone"):
            print(f"{name}: unreadable answer from {host} (exit {r.returncode}): {(r.stdout + r.stderr).strip()[-200:]}")
            return 2
        if out[0] == "rc":
            print(f"{name}: ended, exit {out[1].strip() if len(out) > 1 and out[1].strip() else '?'}")
        elif out[0] == "running":
            ended = False
            print(f"{name}: running on {host}")
        elif out[0] == "none":
            print(f"{name}: gone without an exit code: no driver was recorded at {pre} on {host}")
        else:
            why = "the host restarted" if out[0] == "boot" else "killed, or the host restarted"
            print(f"{name}: gone without an exit code ({why}); see {pre}.log on {host}")
    return 0 if ended else 1


def cmd_devq(a) -> None:
    """The project's serial device-job runners (config `device.runners.<name>`; see devq.py).

    `ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] [--workdir <dir>] [--input <path>]...
    [--cold-start <s>] [--no-lint] -- <command>` lints the command first (devq.lint: bash -n, the scripts it
    calls exist on the host, a task id not this task's; --no-lint skips it), then
    queues one job on the runner's host (whose job guard, devq.guard, may refuse it; it checks the --input
    paths are readable there and warns on a --cold-start longer than the job's limit) and starts the runner if it is down (with runner.device_timeout_max_s
    set, it refuses a longer --timeout and gives a job without one that ceiling); it prints the retry_when,
    `ttp devq probe <runner> <id>`, which exits 0 once the job has its done marker, 1 while it runs and
    3 while it waits with no runner alive (the daemon then runs `ttp devq start <runner>` itself, model-free,
    and the task sleeps on: daemon._restart_runner). `status <runner> [<id>]` shows the queue or one job's marker; `clear <runner> <config>`
    allows a config skipped for its drops again; `list` names the runners."""
    from . import devq
    p = here()
    runners = (((p.config().get("device") or {}).get("runners")) or {}) if p else {}
    if a.op == "list":
        if not runners:
            print("no device runners configured (config device.runners)")
        for name, rc in sorted(runners.items()):
            s = devq.settings(rc)
            print(f"{name}\t{s['host'] or 'this machine'}:{devq.runner_dir(name, s)}")
        return
    if not p:
        die("no tt-project project here")
    if not a.runner or a.runner not in runners:
        die(f"no device runner named {a.runner!r}; configured: {', '.join(sorted(runners)) or 'none'}")
    rc = runners[a.runner]
    bad = devq.config_problems(a.runner, rc)
    if bad:
        die("; ".join(bad))
    cfg_json = json.dumps(devq.settings(rc))
    rest = list(a.rest or [])
    if a.op == "submit":
        usage = ("usage: ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] [--workdir <dir>] "
                 "[--input <path>]... [--cold-start <s>] [--no-lint] -- <command...>")
        opts, rest = (rest[:rest.index("--")], rest[rest.index("--") + 1:]) if "--" in rest else (rest, [])
        sp = argparse.ArgumentParser(prog="ttp devq submit", add_help=False)
        for flag in ("--id", "--config", "--workdir"):
            sp.add_argument(flag, default="")
        sp.add_argument("--timeout", type=int, default=0)
        sp.add_argument("--input", action="append", default=[])
        sp.add_argument("--cold-start", type=int, default=0)
        sp.add_argument("--no-lint", action="store_true")
        try:
            o, extra = sp.parse_known_args(opts)
        except SystemExit:
            die(usage)
        if not o.id or not rest or extra:
            die(usage)
        a.id = o.id
        timeout = o.timeout
        ceiling = device_timeout_max(p.config())[0]
        if ceiling and timeout > ceiling:
            die(f"devq submit: --timeout {timeout} is above the project's device-job ceiling of {ceiling} s "
                f"(runner.device_timeout_max_s); split the run into shorter jobs")
        if ceiling and not timeout and not 0 < float(devq.settings(rc)["job_timeout_s"] or 0) <= ceiling:
            timeout = ceiling
            print(f"devq submit: no --timeout; the job gets the project's device-job ceiling of {ceiling} s")
        spec = {"id": o.id, "config": o.config or o.id, "cmd": rest[0] if len(rest) == 1 else shlex.join(rest),
                "task": os.environ.get("TTP_TASK", ""), "workdir": o.workdir, "timeout_s": timeout,
                "inputs": o.input, "cold_start_s": o.cold_start}
        if not o.no_lint:
            errors, warnings = devq.lint(spec["cmd"], rc, o.id, o.workdir, spec["task"])
            for w in warnings:
                print(f"devq submit: warning: {w}", file=sys.stderr)
            if errors:
                die("devq submit: refused, the job would fail at once: " + "; ".join(errors)
                    + " (fix the command, or pass --no-lint if this check is wrong)")
        args, install, limit = [cfg_json, json.dumps(spec)], True, 120
    elif a.op == "start":
        args, install, limit = [cfg_json], True, 120
    elif a.op in ("probe", "clear"):
        if len(rest) != 1:
            die(f"usage: ttp devq {a.op} <runner> <{'id' if a.op == 'probe' else 'config'}>")
        args, install, limit = rest, False, 50 if a.op == "probe" else 120
    else:
        args, install, limit = rest[:1], False, 120
    argv = devq.host_call(a.runner, rc, a.op, args, install)
    try:
        r = subprocess.run(argv, input=devq.source(), text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"ttp devq: the runner's host did not answer within {limit} s", file=sys.stderr)
        sys.exit(75 if a.op == "probe" else 1)
    if a.op == "submit" and r.returncode == 0:
        remote = bool(devq.settings(rc)["host"])
        print(f"hand off: status waiting, retry_when \"{_own_ttp_word(p)} devq probe {a.runner} {a.id}\", "
              f"wake_tier light{', survives_reboot true' if remote else ''}")
    sys.exit(r.returncode)


def _own_ttp_word(p: Project | None) -> str:
    from .push import _own_ttp
    return _own_ttp(p)


# operating ----------------------------------------------------------------------------------------
def cmd_ci(a) -> None:
    """CI probe for `retry_when`: exit 0 once the commit's GitHub Actions runs completed or one of
    their jobs hung (in progress past 3x its recent median), 1 while they run, 75 when gh cannot
    answer. Prints one line per run (`done:`, `hung:`, `running:`); see ciwait."""
    from . import ciwait
    rc, lines = ciwait.probe(repo=a.repo, branch=a.branch, sha=a.sha, workflow=a.workflow, run=a.run,
                             hang=not a.no_hang, factor=a.factor, floor_min=a.floor_min, default_min=a.default_min)
    for line in lines:
        print(line)
    sys.exit(rc)


def cmd_list(a) -> None:
    if getattr(a, "status", False):
        return cmd_overview(a)
    reg = load_registry().get("projects", {})
    if not reg:
        print("no projects registered on this machine")
    for name, e in sorted(reg.items()):
        print(f"{name}\t{e.get('host')}:{e.get('dir')}")


def cmd_overview(a) -> None:
    """Every project on this machine at a glance: daemon, harness version, asks, waiting, running,
    spend today; other projects are only read (overview.py)."""
    from . import overview as ov
    data = ov.overview()
    print(json.dumps(data, indent=1, default=str) if getattr(a, "json", False) else ov.text(data))


def cmd_deployed(a) -> None:
    """Probe for a deploy (and a retry_when): exit 0 once every project `ttp list` shows on this machine
    runs the target version and commit by its daemon's own receipt and completed a tick since; 1 while
    any is pending (a deferred merge, a restart not yet done), one line each with owner and resume time;
    2 when it cannot tell. Read only unless --after-push records owned pending merges and alerts
    in the reporting project (done/pending exit 0, failed exit 1)."""
    from . import overview as ov
    names = [n.strip() for n in (a.projects or "").split(",") if n.strip()] or None
    data = ov.deployed(a.version, a.commit, names, a.fresh_s, allow_newer=a.after_push)
    if a.after_push:
        from . import deploy
        p = here()
        if not p:
            die("--after-push needs a reporting project (TTP_PROJECT or the current folder)")
        data = deploy.record(p, data)
    print(json.dumps(data, indent=1, default=str) if a.json else data["text"])
    sys.exit(data["rc"])


def cmd_find(a) -> None:
    p, entry = resolve(a.name)
    if p:
        print(MARKER.format(name=a.name, host=hostname(), dir=p.root))
    elif entry:
        print(MARKER.format(name=a.name, host=entry["host"], dir=entry["dir"]))
    else:
        die(f"not found in the registry or in this machine's chat logs. Ask the user where it runs, "
            f"then `ttp adopt {a.name} --host HOST --dir DIR`", 1)


def cmd_adopt(a) -> None:
    register(a.name, {"host": a.host or hostname(), "dir": a.dir})
    print(MARKER.format(name=a.name, host=a.host or hostname(), dir=a.dir))


def cmd_task(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.action != "add" and (a.start_when, a.start_after, a.why) != (None, None, None):
        die("--start-when, --start-after and --why go with add; for a queued task use set-when")
    if a.action == "add":
        try:
            tid, note = add_task(p, a)
        except ValueError as e:
            die(str(e))
        print(f"task #{tid} queued" + note)
    elif a.action == "list":
        for t in p.db.q("SELECT id,status,kind,result,tier,title FROM tasks ORDER BY id DESC LIMIT 50"):
            print(f"#{t['id']}\t{task_outcome(t)}\t{t['tier']}\t{t['title']}")
    elif a.action == "cancel":
        from .runner import stop_runs
        tid = int(a.title)
        p.db.update_task(tid, status="cancelled")
        runs = stop_runs(p.db, p.runs, tid)
        print("cancelled" + (f"; ending its running run{'s' if len(runs) > 1 else ''} "
                             f"{', '.join(map(str, runs))}" if runs else ""))
    elif a.action == "requeue":
        if not a.title.strip().isdigit():
            die(f"requeue needs a task id, not {a.title!r}")
        why = p.db.requeue(int(a.title))
        if why:
            die(why)
        print(f"task #{int(a.title)} queued")
    elif a.action == "set-when":
        if not a.title.strip().isdigit():
            die(f"set-when needs a task id, not {a.title!r}")
        try:
            print(set_when(p.db, int(a.title), a.probe, p))
        except ValueError as e:
            die(str(e))
    elif a.action == "superseded":
        from .landed import record_superseded
        if not a.title.strip().isdigit() or not a.probe:
            die("superseded needs a task id and the commit on the push branch that carries its work")
        try:
            print(record_superseded(p, int(a.title), a.probe.strip()))
        except ValueError as e:
            die(str(e))


def add_task(p, a) -> tuple[int, str]:
    """`ttp task add`: the task and its deferral (--start-after, --start-when, --why) in one insert, so
    the daemon can never start it before its probe is set. Same checks as the coordinator's task_add."""
    from .coordinator import _start_args, defer_labels, marker_only_probe, start_why
    if not a.title.strip():
        raise ValueError("add needs a title")
    d = {"start_after": a.start_after, "start_when": a.start_when, "why": a.why}
    if a.why is not None and a.start_when is None:
        raise ValueError("--why explains a --start-when probe; give one")
    after, when = _start_args(d, {}, p)
    tid = p.db.add_task(a.title, a.spec or "", kind=a.kind, tier=a.tier, priority=a.priority, origin="user",
                        labels=defer_labels(after, when, start_why(d, {}, when)), not_before=after)
    stale = marker_only_probe(when)
    return tid, (f"; starts when: {when}" if when else "") \
        + (f"; starts after {timefmt.long(after, p)}" if after else "") + (f"; flagged: {stale}" if stale else "")


def set_when(db, tid: int, probe: str | None, p=None) -> str:
    """Re-point the probe of a task that has not started: a waiting task's `retry_when`, else its
    `start_when` deferral. An empty probe clears it. Same checks as the coordinator's task_update
    (the push branch's log check needs `p`)."""
    from .coordinator import _start_args, check_probe, check_task_refs, defer_labels, marker_only_probe, start_why
    from .db import TERMINAL_TASK_STATES, deferral, dump_result, load_result, without_deferral
    if probe is None:
        raise ValueError("set-when needs the probe command (\"\" clears it)")
    with db.tx():   # BEGIN IMMEDIATE: the daemon cannot start the task between the check and the write
        task = db.task(tid)
        if not task:
            raise ValueError(f"no task #{tid}")
        if task["status"] in ("running", *TERMINAL_TASK_STATES):
            raise ValueError(f"task #{tid} is {task['status']}: only a task that has not started can have "
                             f"its probe changed; add a new one instead")
        prev = load_result(task["result"])
        if prev.get("status") == "waiting" and not deferral(task).get("when"):
            check_probe(probe, "retry_when")
            check_task_refs(db, probe, "retry_when")
            if probe.strip():
                prev["retry_when"] = probe.strip()
            else:
                prev.pop("retry_when", None)
            db.update_task(tid, result=dump_result(prev))
            return f"task #{tid} retry_when " + (f"set: {probe.strip()}" if probe.strip()
                                                 else "cleared; it wakes at its retry timer")
        cur = deferral(task)
        after, when = _start_args({"start_when": probe}, cur, p)
        db.update_task(tid, labels=without_deferral(json.loads(task["labels"] or "[]"))
                       + defer_labels(after, when, start_why({}, cur, when)), not_before=after)
        stale = marker_only_probe(when) if when != cur.get("when") else None
        return f"task #{tid} start_when " + (f"set: {when}" if when else "cleared") \
            + (f"; flagged: {stale}" if stale else "")


def cmd_prune(a) -> None:
    """One sweep over finished tasks' worktrees, with the daemon's checks: clear build and cache
    directories, move small untracked leftovers to the task's last run directory, remove the worktree
    when nothing is lost. Branches stay. --dry-run changes nothing and lists what would happen."""
    from . import worktree
    p = need(a.name, sys.argv[1:])
    dry = getattr(a, "dry_run", False)
    disk = p.config().get("disk", {})
    before = shutil.disk_usage(p.worktrees).free if p.worktrees.is_dir() else 0
    res = worktree.sweep(p, names=disk.get("cache_dirs"), leftovers_max_mb=disk.get("worktree_leftovers_max_mb"),
                         dry_run=dry)
    for r in res:
        cleared = f"; cleared {', '.join(r['cleared'][:5])}" if r["cleared"] else ""
        mv = r.get("moved")
        moved = (f"{'would move' if dry else 'moved'} {mv['files']} untracked file(s) ({mv['bytes'] / 1e6:.1f} MB) "
                 f"to {mv['to']}; " if mv else "")
        print(f"#{r['task']} ({r['status']}): " + (moved + ("would be removed" if dry else "removed") + ", branch "
                                                   + (r["branch"] or "?") + " kept"
                                                   if r["why"] is None else f"kept: {r['why']}") + cleared)
    if not res:
        print("no finished task's worktree to tidy")
    elif before and not dry:
        print(f"freed {max(shutil.disk_usage(p.worktrees).free - before, 0) / 1e9:.1f} GB")


def cmd_memory(a) -> None:
    from .coordinator import memory_budget_check
    p = need(a.name, sys.argv[1:])
    if not a.text and not a.forget:
        sys.exit("give the memory's text, or --forget <entry>")
    if a.text:
        print(p.add_memory(a.text, kind=a.kind, standing=a.standing))
    for name in a.forget or []:
        try:
            print(f"retired to {p.forget_memory(name, why=a.why)}")
        except ValueError as e:
            sys.exit(str(e))
    memory_budget_check(p)


def cmd_schedules(a) -> None:
    """Read-only by default (list, show). Only --export writes: it moves the schedules into
    harness/schedules.json and commits it, so it is confirmed first."""
    p = need(a.name, sys.argv[1:])
    path = sched.file_path(p)
    if a.export:
        if path.exists():
            die(f"{path} already exists: it holds the schedules; edit it, the daemon applies it")
        if not a.yes:
            note = (f"--export moves the schedules into {path} and commits it: from then on that file, "
                    "not the database, holds them and every change is a harness commit. "
                    "To only read them, use `ttp schedules {} show <schedule>`.".format(p.name))
            if not sys.stdin.isatty():
                die(note + " Add --yes to export anyway.")
            print(note)
            if input("export? [y/N] ").strip().lower() not in ("y", "yes"):
                print("not exported")
                return
        sched.write_file(p, "schedules: exported from the database", create=True)
        print(f"moved the schedules into {path} and committed it; from now on it holds them "
              "and every change to them is a harness commit")
        return
    if a.action == "show":
        if not a.schedule:
            die("usage: ttp schedules <name> show <schedule>")
        r = p.db.one("SELECT * FROM schedules WHERE name=?", (a.schedule,))
        if not r:
            die(f"no schedule {a.schedule!r}")
        r = dict(r)
        e = sched.entry(r)
        state = {k: r[k] for k in r if k not in ("name", "kind", "every_s", "at", "enabled", "budget_usd_day",
                                                  "description", "payload")}
        print(json.dumps({**e, "state": state}, indent=2, default=str))
        return
    print(f"schedules from {path}" if path.exists() else
          f"schedules from the database only (`ttp schedules {p.name} --export` moves them into {path})")
    home = timefmt.home(p)
    for r in p.db.q("SELECT * FROM schedules ORDER BY name"):
        e = sched.entry(r)
        t = e["payload"].get("timeout_s") if isinstance(e["payload"], dict) else None
        at = sched.at_text(r, home)
        print(f"  {e['name']:24} {e['kind']:8} every {e['every']}{' ' + at if at else ''}"
              f"{f' timeout {t}s' if t else ''}{'' if e['enabled'] else ' (off)'}  {r['last_status'] or ''}")


def cmd_heal(a) -> None:
    """Heal checks (command schedules with a `heal` block): `list` shows each with its state,
    `test <name>` runs its check once as a dry run (no fix, no state change)."""
    from . import heal, responsibilities
    if a.project:
        p = need(a.project, sys.argv[1:])
    else:
        base = os.environ.get("TTP_PROJECT") or next(
            (str(d) for d in [Path.cwd(), *Path.cwd().parents] if Project(d).exists()), None)
        if not base:
            die("no project here: run it inside a project or a run, or pass --project NAME")
        p = Project(base)
    found = heal.checks(p.db)
    if a.action == "list":
        if not found:
            print("no heal checks (a command schedule's `heal` block declares one)")
        try:
            items = responsibilities.inventory(p)
        except Exception:
            items = None
        cover = responsibilities.check_cover(items or [])
        for r, spec in found:
            print(heal.describe(r, spec, p.db))
            if items is not None:
                hint = "" if cover.get(r["name"]) else responsibilities.uncovered_hint(items)
                print(f"    covers: {', '.join(cover.get(r['name']) or ['no inventory item'])}"
                      + (f" ({hint})" if hint else ""))
        line = heal.line(p.db)
        if line:
            print(line)
        return
    match = [(r, spec) for r, spec in found if r["name"] == a.check]
    if not match:
        die(f"no enabled heal check named {a.check!r}; `ttp heal list` shows them")
    r, spec = match[0]
    rc, out = heal.check(spec, dict(heal.state(p.db, r["name"])), str(p.root), command_env(p))
    word = "unhealthy, not an outage" if heal.finding(rc, out) else {0: "healthy", 1: "unhealthy"}.get(rc, "unknown")
    print(f"{r['name']}: {word} (exit {rc})" + (f"\n{out}" if out else ""))
    sys.exit(0 if rc == 0 else 1 if rc == 1 or heal.finding(rc, out) else 75)


def cmd_obligation(a) -> None:
    """`ttp obligation add|attach|progress|receipt|drop|list`: urgent deliverables owed to the user (obligations.py).
    Inside a run, progress and receipts go to the run's file and the daemon files them for its task."""
    from . import obligations as ob
    if a.milestone and a.action != "receipt":
        die("--milestone is for a checked milestone receipt")
    run_dir = os.environ.get("TTP_RUN_DIR")
    if run_dir and a.action in ("progress", "receipt"):
        task = os.environ.get("TTP_TASK") or ""
        if not task.isdigit():
            die(f"ttp obligation {a.action}: this run has no task (TTP_TASK)")
        if len(a.args) != 2 or not a.args[1].strip():
            die(f"usage: ttp obligation {a.action} <id> \"<evidence>\"")
        ob.append_run(Path(run_dir), a.args[0], a.action, a.args[1], task, milestone=a.milestone)
        print(f"{a.action} on {a.args[0]} recorded for task #{task}; the daemon files it within "
              f"{ob.CHECK_EVERY_S // 60} min (only if this task owns {a.args[0]})")
        return
    if in_run():
        die(f"ttp obligation {a.action} is for the user or the coordinator, not inside a run; an owner "
            f"records `progress` and `receipt`")
    p = need(a.project, sys.argv[1:]) if a.project else here()
    if not p:
        die("no tt-project project here; name one with --project")
    try:
        if a.action == "add":
            if len(a.args) != 1:
                die('usage: ttp obligation add "<what is owed>" [--owner <task>]...')
            oid = ob.add(p, a.args[0], [int(t) for t in a.owner or []])
            print(f"obligation {oid} recorded" + (f"; owners: {', '.join('#' + str(t) for t in a.owner)}"
                                                  if a.owner else "; attach its owner tasks with `ttp obligation attach`"))
        elif a.action == "attach":
            if len(a.args) < 2 or not all(x.lstrip("#").isdigit() for x in a.args[1:]):
                die("usage: ttp obligation attach <id> <task>...")
            new = ob.attach(p, a.args[0], [int(x.lstrip("#")) for x in a.args[1:]])
            print(f"attached to {', '.join('#' + str(t) for t in new)}" if new else "already attached")
        elif a.action in ("progress", "receipt"):
            if len(a.args) != 2:
                die(f"usage: ttp obligation {a.action} <id> \"<evidence>\" [--task <owner>]")
            print(ob.record(p, a.args[0], a.action, a.args[1], a.task, milestone=a.milestone))
        elif a.action == "drop":
            if len(a.args) != 2:
                die('usage: ttp obligation drop <id> "<why it is no longer owed>"')
            print(ob.drop(p, a.args[0], a.args[1]))
        elif a.json:
            print(json.dumps(ob.items(p.db), indent=1))
        else:
            print("\n".join(ob.lines(p.db, where=p)) or "no obligations")
    except ValueError as e:
        die(str(e))


def cmd_machines(a) -> None:
    """The user's machines (~/.tt-project/machines.json), shared by all their projects. Each
    project's charter says which of them it may use; its coordinator routes work only to those.
    A project created with --host reads the copy on its machine: changes are copied there, merged."""
    from . import machines as mm
    if a.action == "change":
        return _machine_change(a)
    if a.action == "outage":
        return _machine_outage(a)
    if a.action == "status":
        from . import responsibilities
        view = responsibilities.machine_status(a.alias)
        if a.json:
            print(json.dumps(view, indent=2, sort_keys=True))
        elif not view:
            print(f"no machines in {mm.path()} (ttp machines add <alias>)")
        for rec in ([] if a.json else view):
            print("\n".join(responsibilities.machine_lines(rec)))
        return
    if a.action == "set":
        a.action = "add"
    if a.action == "add":
        recovery = a.owner is not None or a.fallback is not None
        try:
            if not recovery or any(v is not None for v in (a.tags, a.note, a.until, a.min_free_gb, a.hostname,
                                                            a.shared)) or a.unshared:
                entry = mm.add(a.alias, a.tags, a.note,
                               ... if a.min_free_gb is None else a.min_free_gb, a.hostname,
                               False if a.unshared else a.shared, a.until)
            if recovery:   # only an owner or fallback: the machine must be known already
                entry = mm.set_recovery(a.alias.strip(), a.owner, a.fallback)
        except ValueError as e:
            die(str(e))
        print(f"saved {mm.line(a.alias.strip(), entry)}")
    elif a.action == "remove":
        try:
            if not mm.remove(a.alias):
                die(f"no machine {a.alias!r} in {mm.path()}")
        except ValueError as e:
            die(str(e))
        print(f"removed {a.alias}")
    if a.action in ("add", "remove", "push", "set"):
        hosts = [a.host] if getattr(a, "host", None) else remote_hosts()
        if a.action == "push" and not hosts:
            print("no projects on other machines; nothing to copy")
        for host in hosts:
            print(mm.push(host))
    else:
        known = mm.load()
        if a.json:
            print(json.dumps(known, indent=2, sort_keys=True))
            return
        if not known:
            print("no machines yet: ttp machines add <alias> --tags device,... [--note TEXT]")
        for alias in sorted(known):
            print(mm.line(alias, known[alias]))


def _machine_outage(a) -> None:
    """`ttp machines outage`: a known outage (hardware repair, an external ticket) that pages nobody."""
    from . import machine_ledger as ml
    if a.clear:
        if not a.alias:
            die("name the machine whose outage is over")
        print(f"{a.alias}: known outage cleared" if ml.clear_outage(a.alias) else f"{a.alias}: no known outage")
        return
    if not a.why:
        got = ml.outages()
        for alias, o in sorted(got.items()):
            if not a.alias or alias == a.alias:
                print(ml.outage_line(alias, o))
        if not got:
            print("no known outages")
        return
    p = here()
    try:
        rec = ml.set_outage(a.alias, a.why, a.by or (p.name if p else "user"), a.expires, owner=a.owner or "")
    except ValueError as e:
        die(str(e))
    print(ml.outage_line(a.alias, rec))


def _machine_change(a) -> None:
    """`ttp machines change add|list|close`: the ledger of live changes to shared machines (machine_ledger.py)."""
    from . import machine_ledger as ml
    if a.change == "list":
        got = ml.changes([a.alias] if a.alias else None, open_only=not a.all)
        if a.json:
            print(json.dumps(got, indent=2, sort_keys=True))
            return
        if not got:
            print("no open machine changes")
        for c in got:
            print(ml.change_line(c) + (f" (closed {ml.ends.stamp(c['closed'])}: {c.get('closed_why')})"
                                       if c.get("closed") else ""))
        return
    p = here()
    by = a.by or (p.name if p else "")
    if a.change == "close":
        try:
            c = ml.close_change(a.id, a.why or "", by or "user")
        except ValueError as e:
            die(str(e))
        print(f"closed machine change #{c['id']} on {c['alias']}")
        return
    if not by:
        die("run it inside a project, or say whose change it is: --by <project>")
    task = os.environ.get("TTP_TASK")
    tid = int(task) if task and task.isdigit() else None
    if a.power_cycle_at and (a.undo or a.expires or a.until_probe):
        die("a power cycle needs no --undo, --expires or --until-probe: it closes itself after its time")
    if not a.power_cycle_at and not a.undo:
        die("give the --undo command (or --power-cycle-at <t> for a planned power cycle)")
    if not a.power_cycle_at and not (a.expires or a.until_probe):
        die("give an end: --expires <delay or time> or --until-probe <command>")
    try:
        c = ml.add_power_cycle(a.alias, a.power_cycle_at, by, a.what, tid) if a.power_cycle_at else \
            ml.add_change(a.alias, a.what, a.undo, by, a.expires, a.until_probe, tid, expects=a.expect)
    except ValueError as e:
        die(str(e))
    print(f"recorded {ml.change_line(c)}")


def cmd_pause(a) -> None:
    p = need(a.name, sys.argv[1:])
    if a.resource and getattr(a, "until_reboot", False):
        die("--until-reboot pauses the whole project; a resource pause ends with --until or --end-when")
    if a.resource:
        from .coordinator import pause_resource
        try:
            from . import pauseends
            end = pauseends.from_action({"until": a.until, "end_when": a.end_when, "report_from": a.report_from}) \
                if a.cmd == "pause" else None
            print(f"{p.name}: " + pause_resource(p, a.resource, a.cmd == "pause", reason=getattr(a, "reason", None) or "",
                                                 by="user", end=end))
        except ValueError as e:
            die(str(e))
        return
    boot = None
    if a.cmd == "pause" and getattr(a, "until_reboot", False):
        from .runner import boot_id
        boot = boot_id()
        if boot == "unknown":
            die("--until-reboot: this host's boot id cannot be read (no /proc/sys/kernel/random/boot_id "
                "or kern.bootsessionuuid or kern.boottime); use a plain pause and resume it after the reboot")
    p.db.set_paused(a.cmd == "pause", boot)
    print(f"{p.name} " + ("resumed" if a.cmd != "pause" else "paused: no new model runs start"
                          + (" until the host reboots; the daemon lifts it on its first tick after boot" if boot else "")))


def _draining_runs(p: Project) -> list[dict]:
    """Runs and pushes in flight in project p, apart from the run calling (TTP_RUN_ID of this project)."""
    me = os.environ.get("TTP_RUN_ID") if os.environ.get("TTP_PROJECT") and \
        Path(os.environ["TTP_PROJECT"]).resolve() == Path(p.base).resolve() else None
    runs = [dict(r) for r in p.db.q("SELECT id,task,role FROM runs WHERE status='running' ORDER BY id")
            if str(r["id"]) != str(me)]
    return runs + [{"id": None, "task": t["id"], "role": "push"}
                   for t in p.db.q("SELECT id FROM tasks WHERE status='pushing' ORDER BY id")]


def cmd_drain(a) -> None:
    """Pause the project (no new runs) and wait until the runs in flight end; running workers are kept.
    Its `ttp detach` jobs still running here are listed (a reboot ends them); with --jobs it waits for
    them too. With --reboot it records the planned host cycle (hostcycle), so the next boot does not
    count the jobs it cuts as the tasks' reboot losses."""
    from . import hostcycle
    p = need(a.name, sys.argv[1:])
    if not p.db.kv("paused", False):
        p.db.set_paused(True)
        print(f"{p.name} paused: no new model runs start")
    end = time.time() + max(0.0, a.wait)
    while True:
        left, jobs = _draining_runs(p), hostcycle.running_jobs(p)
        if not left and not (a.jobs and jobs) or time.time() >= end:
            break
        time.sleep(min(poll_s(2.0), max(0.1, end - time.time())))
    if a.reboot:
        print(f"planned host cycle recorded: {hostcycle.record(p, a.reboot, left, jobs)}")
    if jobs:
        print(f"{p.name}: {len(jobs)} detached job(s) still running on this host; a reboot ends them"
              + (" (recorded as interrupted by the planned cycle)" if a.reboot else "") + f": {hostcycle.describe(jobs)}",
              file=sys.stderr if a.jobs else sys.stdout)
    if not left and not (a.jobs and jobs):
        print(f"{p.name} drained: no runs in flight")
        return
    what = ", ".join(f"push of task #{r['task']}" if r["role"] == "push" else
                     f"run {r['id']} ({r['role']}" + (f", task #{r['task']})" if r["task"] else ")") for r in left)
    if a.jobs and jobs:
        what = ", ".join(x for x in (what, f"{len(jobs)} detached job(s)") if x)
    print(f"{p.name} not drained after {a.wait:g} s: {len(left) + (len(jobs) if a.jobs else 0)} in flight: {what}"
          + f". The project stays paused; `ttp resume {p.name}` lifts it", file=sys.stderr)
    sys.exit(1)


def cmd_service(a) -> None:
    p = need(a.name, sys.argv[1:])
    from . import service
    if a.cmd == "start":
        print(service.install(p))
    elif a.cmd == "stop":
        print(service.uninstall(p))
        pid = p.state / "daemon.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 15)
            except (OSError, ValueError):
                pass
        print(stop_workers(p, a.kill))
    else:
        _exit_for_restart(service.restart(p))


def stop_workers(p: Project, kill: bool, wait_s: float = 60) -> str:
    """Without kill, running workers finish on their own and the next start records their results.
    With kill, they end now (TERM, then KILL); their tasks go back to the queue and resume later."""
    from .runner import stop_runs
    running = p.db.q("SELECT id FROM runs WHERE status='running'")
    if not running:
        return "no runs in progress"
    if not kill:
        return (f"{len(running)} run(s) in progress keep going; the next start records their results. "
                f"`ttp stop {p.name} --kill` ends them.")
    ids = stop_runs(p.db, p.runs, why="shutdown")
    deadline = time.time() + wait_s
    left = list(ids)
    while left and time.time() < deadline:
        time.sleep(poll_s(1))
        left = [i for i in left if _run_dir_alive(p, i)]
    if left:
        return f"ending {len(ids)} run(s); still ending: {', '.join(map(str, left))}"
    return f"ended {len(ids)} run(s); their tasks resume on the next start"


def _run_dir_alive(p: Project, run_id: int) -> bool:
    row = p.db.one("SELECT dir FROM runs WHERE id=?", (run_id,))
    d = Path(row["dir"]) if row and row["dir"] else p.runs / str(run_id)
    if (d / "exit.json").exists():
        return False
    try:
        return time.time() - (d / "lease").stat().st_mtime <= 180
    except OSError:
        return False


def cmd_account_config(key: str, value: str | None) -> None:
    """Read or set an account-level setting (project.ACCOUNT_KEYS) in ~/.tt-project/settings.json."""
    if value is None:
        node = deep_merge(DEFAULT_CONFIG, load_account_settings())
        for part in key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(value)
    except ValueError:
        val = value
    from .globalcap import setting_problems
    sec, _, k = key.partition(".")
    bad = setting_problems({k: val}) if sec == "budget" and val not in ("", None) else []
    if bad:
        die(bad[0])
    try:
        set_account_setting(key, None if val == "" else val)
    except ValueError as e:
        die(str(e))
    print(f"{key} = {json.dumps(val)} (account-level, every project on this machine)" if val != ""
          else f"{key} removed (account-level); the default applies")


def _user_only_change(raw: dict, key: str, val) -> str | None:
    """The user-only setting (coordinator.NEEDS_USER) that writing `val` at `key` into the project.json
    `raw` would change, or None. A parent key (`budget`, `delivery`) counts through what it holds, and
    turning a SAFE_WHEN_OFF switch off is the safe direction, as in the coordinator's own gate."""
    import copy
    from .coordinator import NEEDS_USER, SAFE_WHEN_OFF

    def at(node, dotted):
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    after = copy.deepcopy(raw)
    node = after
    for part in key.split(".")[:-1]:
        nxt = node.get(part) if isinstance(node, dict) else None
        node = node.setdefault(part, {}) if isinstance(nxt, dict) or nxt is None else None
        if node is None:   # set_config fails on a scalar parent; nothing user-only changes here
            return None
    node[key.split(".")[-1]] = val
    for k in NEEDS_USER:
        if k != key and not k.startswith(key + ".") and not key.startswith(k + "."):
            continue
        new = at(after, k)
        if new != at(raw, k) and not (k in SAFE_WHEN_OFF and new in (False, "", [])):
            return k
    return None


def _refuse_user_only_in_run(key: str) -> None:
    die(f"{key} is a setting only the user may change, and `ttp config` refuses to set it inside a run; "
        f"reading it still works. Name the value you need in the hand-off (a followup or the question) "
        f"so the coordinator can ask the user")


def cmd_config(a) -> None:
    run = in_run()
    if a.account:
        if a.value is not None:
            die("usage: ttp config --account KEY [VALUE]")
        from .coordinator import NEEDS_USER
        if run and a.key is not None and a.name in NEEDS_USER:
            # A run can be steered by text it reads; spend caps change only on the user's word.
            _refuse_user_only_in_run(a.name)
        return cmd_account_config(a.name, a.key)
    if a.key is None:
        die("usage: ttp config NAME KEY [VALUE]")
    p = need(a.name, sys.argv[1:])
    if a.value is None:
        node = p.config()
        for part in a.key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else None
        print(json.dumps(node, indent=1))
        return
    try:
        val = json.loads(a.value)
    except ValueError:
        val = a.value
    from .project import unknown_key_hint
    hint = unknown_key_hint(a.key)
    if hint:
        die(hint)
    if a.key == "runner.pin_verify":
        from .project import pin_verify_mode
        try:
            val = pin_verify_mode(val)
        except ValueError as e:
            die(str(e))
    if a.key in ("coordinator.provider_failover_after_h", "coordinator.provider_failover_to"):
        from . import failover
        try:
            val = (failover.after_h_of(val) if a.key.endswith("_after_h")
                   else failover.to_of(val, p.config().get("providers")))
        except ValueError as e:
            die(str(e))
    if a.key == "delivery.push_checks":
        from .push import checks_of
        try:
            val = checks_of(val)
        except ValueError as e:
            die(str(e))
    if run and (k := _user_only_change(p.raw_config(), a.key, val)):
        # A run can be steered by text it reads; these change only in a coordinator turn that carries
        # the user's yes (coordinator.NEEDS_USER), as `ttp say` is refused inside a run.
        _refuse_user_only_in_run(k)
    try:
        p.set_config(a.key, val)
    except RuntimeError as e:   # project.json unreadable with no last good copy
        die(str(e))
    print(f"{a.key} = {json.dumps(val)}")


def cmd_secret(a) -> None:
    if a.kind == "jev":
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Jev key: ")
        from .providers.jev import verify
        ok, msg = verify(a.via, key, a.url)
        if not ok and not a.force:
            die(f"the key did not work ({msg}); not saved. Use --force to save anyway.")
        save_secret("jev", {"via": a.via, "key": key, **({"url": a.url} if a.url else {})})
        print(f"saved Jev key for this user ({a.via}; check: {msg})")
    elif a.kind == "slack":
        tok = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Slack bot token (xoxb-…): ")
        from .slack import Slack
        s = Slack(tok, a.user_id, a.email)
        try:
            s.call("auth.test")
            uid = s.resolve_user()
        except Exception as e:
            die(f"Slack check failed: {e}")
        save_secret("slack", {"bot_token": tok, "user_id": uid, "user_email": a.email})
        print(f"saved Slack bot for user {uid}")
    elif a.kind == "push":
        if not a.host:
            die("--host is required")
        print(push_secrets(a.host))
    elif a.kind == "show":
        sec = load_secrets()
        print(json.dumps({k: sorted(v.keys()) if isinstance(v, dict) else "set" for k, v in sec.items()}))


def cmd_logs(a) -> None:
    p = need(a.name, sys.argv[1:])
    f = p.logs / "daemon.log"
    print(f.read_text()[-a.bytes:] if f.exists() else "(no log yet)")


def stale_install_warning(cwd: Path | None = None) -> str:
    """Warn when an installed ttp runs setup inside a checkout that holds a newer plugin."""
    from .project import HOME_DIR
    from .release import is_newer, runtime_version
    try:
        if not RUNTIME.resolve().is_relative_to((HOME_DIR / "lib").resolve()):
            return ""
    except OSError:
        return ""
    here = (cwd or Path.cwd()).resolve()
    for d in (here, *here.parents):
        plugin = d / "plugins" / "tt-project"
        if (plugin / "runtime").is_dir() and (plugin / "bin" / "ttp").exists():
            newer = runtime_version(plugin / "runtime")
            if is_newer(newer, __version__):
                msg = (f"warning: this is the installed ttp {__version__}, but this checkout has {newer}; "
                       f"to install the checkout run `{plugin / 'bin' / 'ttp'} setup`")
                print(msg, file=sys.stderr)
                return msg
            return ""
    return ""


def cmd_setup(a) -> None:
    """Install this runtime as the user's stable `ttp` (plugin caches move on every update), and the plain
    `/tt-project` command for Claude Code (shortcut.py)."""
    from .project import HOME_DIR
    from .release import forced_mark, forced_version, is_newer, runtime_version
    lib = HOME_DIR / "lib" / __version__
    if not (PLUGIN_ROOT / "template").is_dir():
        die(f"{RUNTIME} is a project's harness copy, not the plugin; run `<plugin-root>/bin/ttp setup`", 1)
    installed = runtime_version(HOME_DIR / "lib" / "current" / "runtime")
    if is_newer(installed, __version__) and not a.force:
        die(f"ttp {installed} is installed, newer than this ttp {__version__}: not downgrading it. "
            f"Run setup from the newer plugin, or add --force to install {__version__} anyway", 1)
    stale_install_warning()
    commit = source_commit()
    before = recorded_commit(lib / "runtime") if (lib / "runtime").is_dir() else ""
    if RUNTIME.resolve() != (lib / "runtime").resolve():
        for part in ("runtime", "template", "bin"):
            src = PLUGIN_ROOT / part
            if src.exists():
                _copy_tree(src, lib / part)
        for f in (lib / "bin").iterdir():
            f.chmod(0o755)
    (lib / "runtime" / "ttp" / SOURCE_FILE).write_text(commit + "\n")
    if hasattr(os, "sync"):    # a reboot now must not leave empty files that upgrades take in as upstream
        os.sync()
    # The marker goes first: a daemon check between the two steps must not undo a deliberate downgrade.
    if is_newer(installed, __version__):    # a deliberate downgrade: daemons must not undo it
        durable_write(forced_mark(), __version__ + "\n")
    elif forced_version() != __version__:  # re-running setup of the forced version keeps it forced
        forced_mark().unlink(missing_ok=True)
    cur = HOME_DIR / "lib" / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(lib)
    bindir = Path(a.bin_dir).expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "ttp"
    shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(cur / 'bin' / 'ttp'))} \"$@\"\n")
    shim.chmod(0o755)
    on_path = str(bindir) in os.environ.get("PATH", "").split(":")
    print(f"ttp {__version__} ({commit}) installed: {shim}" + ("" if on_path else f" (add {bindir} to PATH)"))
    if before and before != commit:
        print(f"replaced ttp {__version__} from commit {before} with commit {commit}")
    from . import shortcut
    line = shortcut.remove() if a.no_shortcut else shortcut.install()
    if line:
        print(line)


def cmd_upgrade(a) -> None:
    """Merge the installed template into a project's harness. The harness repo keeps pristine
    template snapshots on its `upstream` branch, so this is an ordinary three-way merge. `--auto` is
    the daemon's own call (release.py): it records the outcome and sends one low notify when applied.
    Exit 75 = deferred, nothing changed and the project finishes it itself (another upgrade or a harness
    task holds it, a push is in flight, or a conflicting merge waits for a harness task or for the daemon's
    retry, which comes only for a newer version under upgrade.auto). Exit 1 = failed, including a conflict
    nothing will retry (upgrade.auto off, or the same version from another commit). After main moved, the
    restart decides: exit 75 = the restart is deferred (the old daemon runs on and restarts when it takes
    the request a sandboxed caller left it), exit 1 = it failed or the runtime was rolled back."""
    from . import locks, release
    if a.project_dir:      # the daemon names its own folder: never another project of the same name
        p = Project(a.project_dir)
        if not p.exists() or p.name != a.name:
            die(f"{a.project_dir} does not hold project {a.name!r}")
    else:
        entry = remote_entry(a.name)
        if entry and not local_project(a.name):
            from . import machines as mm
            ship_runtime(entry.get("ssh") or entry["host"])      # the newer runtime becomes that machine's ttp
            print(mm.push(entry.get("ssh") or entry["host"]))
            sys.exit(forward(entry, sys.argv[1:]))
        p = need(a.name, sys.argv[1:])
    held = locks.try_take([release.upgrade_lock(p)], f"ttp upgrade (pid {os.getpid()})", "ttp upgrade")
    if held is None:
        if a.auto:
            release.finish(p, "held", why="another upgrade is running")
        who = locks.holders([release.upgrade_lock(p)], p)
        die(f"upgrade refused: another upgrade of {p.name} is running"
            + (f" ({who[0]})" if who else "") + "; nothing was changed", 75)
    try:
        own = _own_run(p)
        if a.apply is not None and own and release.fenced(p, os.environ.get("TTP_TASK") or ""):
            die(f"--apply refused: a host-side upgrade took the template merge over from task "
                f"#{os.environ['TTP_TASK']} (its finish-lock generation is older); nothing was changed", 1)
        # Read under the lock: an upgrade that just ended may have queued it. A stale record goes here.
        holder = release.finish_holder(p, clean=True)
        tid = holder["task"] if holder else None
        mine = bool(tid) and os.environ.get("TTP_TASK") == str(tid)
        took, parked = False, None
        if tid and not mine and a.apply is None and not a.auto and not own and p.db.kv("paused", False):
            # A paused project's merge task cannot run: the merge is tried here, files only, and the task
            # is superseded only once it merged cleanly; a conflict leaves it to finish after the resume.
            parked, holder, tid = holder, None, None
        if tid and not mine and a.apply is None and not a.auto and not own:
            took = _take_over_finish(p, holder,     # a blocked or idle task cannot hold off a host-side upgrade
                                     overdue=getattr(a, "take_over_from", None) == tid)
            if took:
                holder, tid = None, None
        if tid and not mine and a.apply is None:    # the open task takes this release on: one merge, not two
            release.guard_harness(p.harness)
            _retarget_upgrade_task(p, holder, a.auto)
        if tid and not mine:      # its worker is resolving the merge: a second resolution would race it
            release.note_deferred(p, task=tid)
            if a.auto:
                release.finish(p, "held", why=f"harness task #{tid} finishes an earlier upgrade")
            die(f"upgrade refused: harness task #{tid} is finishing an earlier template upgrade of {p.name}; "
                f"nothing was changed ({release.holder_line(p, holder)}).", 75)
        if a.apply is None and mine:
            die(f"task #{tid} finishes the merge in its own worktree and applies it with "
                f"`ttp upgrade {p.name} --apply <commit>`", 2)
        release.guard_harness(p.harness)
        if a.apply is not None:
            _apply_upgrade(p, a.apply)
        else:
            _upgrade(p, a.auto, explicit=not a.auto and not own, took_over=took, parked=parked)
    except SystemExit as e:
        if a.auto and e.code and (p.db.kv(release.KV_AUTO) or {}).get("outcome") == "running":
            release.finish(p, "failed", why=f"exit {e.code}; see logs/upgrade.log")
        raise
    except Exception as e:
        if a.auto:
            release.finish(p, "failed", why=f"{type(e).__name__}: {str(e)[:200]}")
        raise
    finally:
        held.close()


def _own_run(p: Project) -> bool:
    """This command runs inside one of `p`'s own runs (a task or coordinator turn of that project)."""
    base = os.environ.get("TTP_PROJECT")
    try:
        return bool(base) and Path(base).resolve() == Path(p.base).resolve()
    except OSError:
        return False


def _take_over_finish(p: Project, holder: dict, overdue: bool = False) -> bool:
    """An explicit host-side upgrade while finish task holder["task"] is blocked or idle past
    release.STALE_S, or (`overdue`: `--take-over-from` names it) past the takeover deadline a deploy gave
    it: fence that task (cancelled, superseded; its `--apply` refused from now on) so this upgrade
    proceeds. True when it took the lock over."""
    from . import release, sweep
    from .project import HOME_DIR
    why = release.stale_holder(p.db, holder) or ("past its deploy takeover deadline" if overdue else "")
    src = HOME_DIR / "lib" / "current" / "runtime"
    if not why or not src.is_dir():
        return False
    tid = holder["task"]
    target = f"{_runtime_version(src)} ({recorded_commit(src)})"
    gen = release.fence(p, tid, target)
    sweep.signal(p.db, f"host upgrade took the template merge over from harness task #{tid} ({why})")
    print(f"harness task #{tid} held the template merge but is {why}; this host-side upgrade takes it over: "
          f"task #{tid} is cancelled (superseded by host upgrade to {target}) and its `--apply` is refused "
          f"(finish-lock generation {gen})")
    return True


def _upgrade(p: Project, auto: bool = False, explicit: bool = False, took_over: bool = False,
             parked: dict | None = None) -> None:
    """`explicit`: run by hand on the host, not --auto and not from one of the project's runs. A conflict
    then queues the merge task once the last one ended upgrade.merge_retry_h ago (`took_over`: at once,
    the last one was just superseded), not only once a day. `parked`: the project is paused and this finish
    task holds the merge: a clean merge supersedes it and lands (the daemon restarts, still paused); a
    conflict retargets it and exits 75, the task finishing it once the project is resumed."""
    from . import release
    from .project import HOME_DIR
    src = HOME_DIR / "lib" / "current"
    if not (src / "runtime").is_dir():
        die("no installed template; run `ttp setup` from the plugin first")
    h = p.harness
    new_v, new_c = _runtime_version(src / "runtime"), recorded_commit(src / "runtime")
    old_v, old_c = _runtime_version(h / "runtime"), recorded_commit(h / "runtime")
    if old_v != new_v:
        print(f"upgrading the harness from ttp {old_v} ({old_c}) to {new_v} ({new_c})")
    elif old_c != new_c:
        print(f"same version {new_v}, new source commit: {old_c} -> {new_c}")
    else:
        print(f"installed template: ttp {new_v} ({new_c})")
    ident = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
    _ensure_git_ident(h)        # harness tasks commit here with plain `git commit`
    _refuse_unfinished_merge(p, auto)
    emptied = _emptied(h, None, "HEAD")
    if emptied:      # a crash cut these short: they are damage, not local edits to commit and keep
        _git(h, "checkout", "HEAD", "--", *emptied)
        print("restored files a crash left empty: " + ", ".join(emptied))
    _restore_cut_runtime(h)
    left = _commit_local_changes(p, ident)
    base = _git(h, "rev-parse", "HEAD")     # the merge is checked against this; main must not move meanwhile
    _snapshot_upstream(p, src, new_v, new_c, ident)
    kept: dict[str, str] = {}
    merged, problem = _merge_upstream(h, p.state / "upgrade-merge", ident, base, kept)
    if problem and parked:      # the open task is retargeted (exit 75); nothing is queued while paused
        print(f"{p.name} paused; upgrades when resumed: the merge needs harness task #{parked['task']}, which "
              f"runs once {p.name} is resumed. Nothing else was changed.")
        _retarget_upgrade_task(p, parked, False)
        sys.exit(75)
    if problem:
        holder = release.finish_holder(p)
        tid = holder["task"] if holder else None
        last = None if tid else release.recent_upgrade_task(p)
        if last and release.landed(p.db, last["id"], old_v, old_c):
            last = None     # it landed its release: this conflict is the next release's, queued at once
        sooner = None       # an explicit host-side upgrade may queue the merge task this soon
        if last and explicit:
            ended = release.task_ended(p.db, last["id"])
            sooner = None if ended is None else ended + release.merge_retry_s(p.config())
            if took_over or (sooner is not None and time.time() >= sooner):
                last = None
        if last:        # at most one model task a day: each release would otherwise queue its own
            until = float(last["created"]) + release.TASK_EVERY_S
            release.note_deferred(p, problem, last["id"])
            release.defer(p, auto, f"{new_v} {new_c}", f"{old_v} ({old_c})", f"{new_v} ({new_c})", until,
                          problem[:300])
            at = timefmt.long(until, p)
            took = (f"upgrade not applied; the running harness is unchanged. {problem}\nHarness task "
                    f"#{last['id']} took on a template merge less than a day ago, so none is queued now; ")
            # Exit 75 (deferred) only when the project takes it on itself: its daemon retries after `until`.
            if sooner is not None and sooner < until:
                at = timefmt.long(sooner, p)    # a host-side rerun queues it before the daemon's daily retry
                took += f"a host-side `ttp upgrade {p.name}` queues one from {at}; "
            if release.retried({"newer": release.is_newer(new_v, old_v)}, p.config()):
                print(took + f"upgrade deferred: owner: this project's daemon (upgrade.auto), which tries again "
                             f"after the day is up; resumes at {timefmt.long(until, p)}.")
                sys.exit(75)
            if p.db.kv("paused", False):    # deferred, not failed: a takeover after the resume queues the merge
                print(took + f"{p.name} paused; upgrades when resumed: a host-side `ttp upgrade {p.name}` after "
                             f"the resume queues the merge task from {at}.")
                sys.exit(75)
            why = ("upgrade.auto is off" if release.is_newer(new_v, old_v)
                   else f"the daemon retries only a newer version, and this is {new_v} again from another commit")
            print(took + f"{why}, so nothing retries it: rerun `ttp upgrade {p.name}` after {at} "
                         f"to queue a harness task for the merge.")
            sys.exit(1)
        target = f"{new_v} ({new_c})"
        tid = tid or p.db.add_task(
            release.UPGRADE_TASK_TITLE, _UPGRADE_TASK.format(problem=problem, name=p.name, target=target,
                                                             state=shlex.quote(str(p.state))),
            kind="harness", tier="standard", priority=2, origin="user")
        release.take_finish_lock(p, tid, f"{new_v} {new_c}", target)     # it holds the live harness until it ends
        release.note_deferred(p, problem, tid)
        if auto:
            release.finish(p, "conflict", task=tid, why=problem[:300])
        print(f"upgrade not applied; the running harness is unchanged. {problem}\nHarness task #{tid} "
              f"finishes it (upgrade deferred to that task).")
        sys.exit(75)        # deferred: the project's own harness task takes it on
    if auto and release.push_in_flight(p):   # a push started while this merged: never swap under it
        release.finish(p, "held", why="a push is in flight")
        print("upgrade held: a push is in flight; the running harness is unchanged and the daemon retries later")
        sys.exit(75)
    moved = _moved_template(h, base)
    if moved:
        if auto:
            release.finish(p, "failed", why=moved[:300])
        die(f"upgrade not applied; the running harness is unchanged. {moved}", 1)
    genv = {**os.environ, release.GUARD_ENV: "1"}
    r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--ff-only", merged], capture_output=True, text=True,
                       env=genv)
    if r.returncode != 0:   # the daemon committed charter or memory meanwhile: those touch other files
        r = subprocess.run(["git", "-C", str(h), *ident, "merge", "--no-edit", merged], capture_output=True,
                           text=True, env=genv)
        if r.returncode != 0:
            subprocess.run(["git", "-C", str(h), "merge", "--abort"], capture_output=True)
            die(f"could not apply the checked upgrade to {h}: {(r.stdout + r.stderr)[-500:]}"
                + (f"; files a running harness task changed were left for it to commit ({', '.join(left[:5])}): "
                   f"rerun `ttp upgrade {p.name}` once it has committed them" if left else ""), 1)
    if hasattr(os, "sync"):
        os.sync()       # a reboot right after must not leave the files git just wrote empty
    if parked:      # merged without its worker: the paused project's finish task has nothing left to do
        release.fence(p, parked["task"], f"{new_v} ({new_c})")
        print(f"{p.name} is paused: merged its files without harness task #{parked['task']}, which is cancelled "
              f"(superseded); the daemon restarts and the project stays paused")
    release.clear_stuck(p)
    print("harness up to date with the installed template; restarting the daemon")
    from . import service
    res = service.restart(p)
    if not auto:
        _exit_for_restart(res)
        return
    print(res)
    if (_runtime_version(h / "runtime"), recorded_commit(h / "runtime")) != (new_v, new_c):
        release.finish(p, "failed", why="the daemon did not start with it, so the runtime was rolled back")
        return
    release.finish(p, "applied", **({"kept_lost": _cut_list(kept)} if kept else {}))
    p.db.post("out", f"tt-project harness upgraded from {old_v} ({old_c}) to {new_v} ({new_c}); the daemon "
              f"restarted and running work was kept."
              + (f" Kept this project's runtime edits that drop names upstream ships: {_cut_list(kept)}." if kept
                 else ""), chat=None, kind="alert", severity="low")


def _running_harness_start(p: Project) -> tuple[float, int] | None:
    """(start, task) of the oldest run of this project's harness tasks still running, leaving out the
    run this command runs in (its worker commits its own files before it upgrades); None when none runs."""
    me = os.environ.get("TTP_RUN_ID") if _own_run(p) else None
    r = p.db.one("SELECT r.started, r.task FROM runs r JOIN tasks t ON t.id=r.task WHERE r.status='running' "
                 "AND t.kind='harness' AND r.started IS NOT NULL AND CAST(r.id AS TEXT) IS NOT ? "
                 "ORDER BY r.started LIMIT 1", (me,))
    return (float(r["started"]), int(r["task"])) if r else None


def _commit_local_changes(p: Project, ident: list[str]) -> list[str]:
    """Commit the harness's local changes before a template upgrade merges into it. While harness tasks of
    the project run, they edit this same checkout and commit with their own message: only changes older
    than the oldest such run's start are committed here, and newer ones (and deletions, whose age is
    unknown) are left for their worker. Waiting for the run instead could hold an upgrade for hours.
    Returns the files left uncommitted."""
    h = p.harness
    if not _git(h, "status", "--porcelain"):
        return []
    msg = "local harness changes before template upgrade"
    running = _running_harness_start(p)
    if not running:
        _git(h, "add", "-A")
        _git(h, *ident, "commit", "-q", "-m", msg)
        return []
    since, tid = running

    def names(*args: str) -> set[str]:
        out = subprocess.run(["git", "-C", str(h), *args], capture_output=True, text=True, check=True).stdout
        return {n for n in out.split("\0") if n}

    changed = (names("ls-files", "-z", "--modified", "--others", "--deleted", "--exclude-standard")
               | names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
    old, left = [], []
    for n in sorted(changed):
        try:
            (old if (h / n).lstat().st_mtime <= since else left).append(n)
        except OSError:
            left.append(n)
    if old:
        spec = p.state / "upgrade-commit-paths"
        spec.write_text("\0".join(old) + "\0")
        try:
            files = ["--pathspec-from-file", str(spec), "--pathspec-file-nul"]
            _git(h, "--literal-pathspecs", "add", "-A", *files)
            # --only: the worker's own staged files stay staged and out of this commit
            if names("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD") & set(old):
                _git(h, "--literal-pathspecs", *ident, "commit", "-q", "--only", "-m", msg, *files)
        finally:
            spec.unlink(missing_ok=True)
    if left:
        print(f"left {len(left)} file(s) changed since harness task #{tid}'s run started for it to commit: "
              f"{', '.join(left[:10])}" + (f" and {len(left) - 10} more" if len(left) > 10 else ""))
    return left


def _snapshot_upstream(p: Project, src: Path, new_v: str, new_c: str, ident: list[str]) -> None:
    """Commit the installed template onto the harness's `upstream` branch (pristine snapshots); main is
    not touched. No commit when upstream already holds it."""
    h = p.harness
    tmp = p.state / "upgrade-wt"
    if tmp.exists():
        shutil.rmtree(tmp)
    own = _own_pytest_config(h)
    _git(h, "worktree", "add", "-q", str(tmp), "upstream")
    try:
        for part, dst in (("runtime", "runtime"), ("template/prompts", "prompts"), ("template/bin", "bin")):
            if (tmp / dst).exists():
                shutil.rmtree(tmp / dst)
            _copy_tree(src / part, tmp / dst)
        for f in HARNESS_ROOT_FILES:
            if (src / "template" / f).is_file() and not (f == "pytest.ini" and own):
                shutil.copy2(src / "template" / f, tmp / f)
        _git(tmp, "add", "-A")
        if _git(tmp, "status", "--porcelain"):
            _git(tmp, *ident, "commit", "-q", "-m", f"tt-project template {new_v} ({new_c})")
    finally:
        _git(h, "worktree", "remove", "--force", str(tmp))


_TARGET_RE = re.compile(r"^Target release: .*$", re.M)


def _retarget_upgrade_task(p: Project, holder: dict, auto: bool) -> None:
    """A `ttp upgrade` while harness task holder["task"] finishes a template merge: the installed
    release goes onto `upstream` (main and the live harness stay as they are) and that task takes it
    on, so one task finishes the merge for every release meanwhile instead of one task each. A running
    worker is told through its steer.md; its `--apply` of a merge without the new upstream is refused.
    Exits 75 (deferred): the open task lands it."""
    from . import release
    from .coordinator import _append_update
    from .project import HOME_DIR
    src = HOME_DIR / "lib" / "current"
    tid = holder["task"]
    if not (src / "runtime").is_dir():
        return
    h = p.harness
    new_v, new_c = _runtime_version(src / "runtime"), recorded_commit(src / "runtime")
    key, target = f"{new_v} {new_c}", f"{new_v} ({new_c})"
    before = _git(h, "rev-parse", "upstream")
    _snapshot_upstream(p, src, new_v, new_c, ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"])
    moved = _git(h, "rev-parse", "upstream") != before
    if holder.get("key") != key:
        task = p.db.task(tid) or {}
        spec = task.get("spec") or ""
        line = f"Target release: tt-project {target}"
        spec = _TARGET_RE.sub(line, spec, 1) if _TARGET_RE.search(spec) else f"{spec.rstrip()}\n\n{line}\n"
        if moved:
            upd = (f"Retargeted to tt-project {target}: `upstream` now holds it. Merge `upstream` again in your "
                   f"worktree (`--apply` refuses a merge without it), resolve, check, then apply.")
            spec += f"\n## Update\n{upd}\n"
            for r in p.db.q("SELECT id, dir FROM runs WHERE task=? AND status='running'", (tid,)):
                if r["dir"]:
                    _append_update(Path(r["dir"], "steer.md"), upd, f"retarget-{tid}-{new_c}")
        # Not through update_task: a retarget is not the task's own progress, and its `updated` time is
        # what tells a stuck finish task (release.stale_holder) from one that moves.
        p.db.x("UPDATE tasks SET spec=? WHERE id=?", (spec, tid))
        release.take_finish_lock(p, tid, key, target)
    release.note_deferred(p, task=tid)
    if auto:
        release.finish(p, "conflict", task=tid, why=f"harness task #{tid} takes this release on")
    print(f"upgrade not applied; the running harness is unchanged. Harness task #{tid} is finishing the "
          f"template merge" + (f" and now targets {target}" if moved or holder.get("key") != key else "")
          + f" (upgrade deferred to that task; {release.holder_line(p, holder)}).")
    sys.exit(75)


def _moved_template(h: Path, base: str) -> str:
    """Why main may not take the checked merge ("" = it may): since `base` someone else changed the
    template's files on it, or merged upstream into it. Only charter, memory and config commits of the
    daemon may land meanwhile; the merge then keeps them. Anything else would be overwritten."""
    head = _git(h, "rev-parse", "HEAD")
    if head == base:
        return ""
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", base, head]).returncode != 0:
        return f"main moved from {base[:12]} to {head[:12]} and no longer contains it"
    touched = _git(h, "diff", "--name-only", base, head, "--", "runtime", "prompts", "bin").split()
    if touched:
        return (f"main moved from {base[:12]} to {head[:12]} while the template was merged, changing "
                f"{', '.join(touched[:5])}; rerun `ttp upgrade` to merge again")
    return ""


def _apply_upgrade(p: Project, commit: str) -> None:
    """`ttp upgrade <name> --apply <commit>`: the last step of an upgrade task. Fast-forwards main to the
    task's checked merge, under the upgrade lock, only when it takes in the current template and main
    has not moved past it; then restarts the daemon like any upgrade."""
    from . import release
    h = p.harness
    r = subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", commit + "^{commit}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"--apply: {commit} is not a commit of {h}", 2)
    merged = r.stdout.strip()
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", "upstream", merged]).returncode != 0:
        die(f"--apply refused: {commit} does not contain the current template (upstream); merge it again", 1)
    if subprocess.run(["git", "-C", str(h), "merge-base", "--is-ancestor", "HEAD", merged]).returncode != 0:
        die(f"--apply refused: main moved and {commit} does not contain it; nothing was changed. In the "
            f"worktree: `git merge main`, check again, commit, then rerun with the new commit.", 1)
    r = subprocess.run(["git", "-C", str(h), "merge", "--ff-only", merged], capture_output=True, text=True,
                       env={**os.environ, release.GUARD_ENV: "1"})
    if r.returncode != 0:
        die(f"--apply: could not fast-forward main to {commit}: {(r.stderr or r.stdout).strip()[-400:]}", 1)
    if hasattr(os, "sync"):
        os.sync()
    release.clear_stuck(p)
    print(f"harness main is now {merged[:12]}; restarting the daemon")
    from . import service
    _exit_for_restart(service.restart(p))


def _exit_for_restart(res) -> None:
    """Print a restart's report; exit 75 when it is deferred (the old daemon runs on and restarts when it
    takes the request) and 1 when it failed or rolled back, never 0 for either."""
    print(res)
    code = {"deferred": 75, "failed": 1, "rolled_back": 1}.get(getattr(res, "outcome", "running"), 0)
    if code:
        print(f"restart {res.outcome.replace('_', ' ')} (exit {code})", file=sys.stderr)
        sys.exit(code)


def _refuse_unfinished_merge(p: Project, auto: bool) -> None:
    """A hand-run `git merge` the guard hook refused leaves MERGE_HEAD and the merged files in the live
    harness. Committing them as local changes is that same merge, so the hook would refuse it again:
    stop here and say what is wrong and how to undo it."""
    from . import release
    h = p.harness
    if subprocess.run(["git", "-C", str(h), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                      capture_output=True).returncode != 0:
        return
    files = _git(h, "diff", "--name-only", "HEAD").splitlines()
    shown = ", ".join(files[:20]) + (f" and {len(files) - 20} more" if len(files) > 20 else "")
    msg = (f"upgrade not applied: an unfinished merge is in the live harness {h} (MERGE_HEAD is set)"
           + (f"; files: {shown}" if shown else "")
           + f". `git -C {shlex.quote(str(h))} merge --abort` restores HEAD; then rerun `ttp upgrade {p.name}`.")
    if auto:            # stderr already goes to logs/upgrade.log; the status line shows the reason
        release.finish(p, "failed", why="an unfinished merge is in the live harness; `git merge --abort` "
                                        "there restores HEAD")
    else:
        p.logs.mkdir(parents=True, exist_ok=True)
        with open(p.logs / "upgrade.log", "a") as log:
            log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} ttp upgrade\n{msg}\n")
    die(msg, 1)


_UPGRADE_TASK = """`ttp upgrade` could not apply the new tt-project template on its own: {problem}

Target release: tt-project {target}

The live harness was left untouched, and this task holds it until it ends: a newer release meanwhile
moves `upstream` and retargets this task (merge `upstream` again) instead of queuing another.
In this harness repo:
1. `git worktree add --detach <tmp> main`, then in <tmp>: `git merge upstream`.
2. Resolve each conflict keeping this project's intent and taking upstream's fixes. Put this project's
   own prompt rules between `<!-- ttp:local -->` and `<!-- /ttp:local -->` lines, so later upgrades keep
   them without a merge.
3. Check in <tmp>: `python3 -m compileall -q runtime` and `PYTHONPATH=runtime python3 -c "import ttp.daemon, ttp.cli"`.
4. Commit, then in <tmp>: `git merge main` (main may have moved meanwhile; resolve and check again).
5. `ttp upgrade {name} --apply <commit>`, run on its own (never piped or masked). It fast-forwards main
   to <commit> under the upgrade lock and restarts the daemon (it rolls the runtime back if the daemon
   does not start). Never merge upstream into main any other way: the harness refuses it.
   Where the service manager is out of reach (a sandbox), it asks the running daemon to restart itself.
   Exit 0: done, remove <tmp>. Exit 75: main is applied and the restart deferred; the old daemon runs on
   and restarts when it takes the request: remove <tmp> and hand off `waiting` with retry_when
   `test ! -e {state}/restart.request -a ! -e {state}/restart.request.taken`. When it wakes you, read
   {state}/restart.result: its `outcome` (running, busy, failed, rolled_back) and `text` say how the
   restart went; report a failure or a rollback with that text.
   Exit 1 or other: keep <tmp> and hand off with its path and the error (it says whether the restart
   was unavailable or the new daemon failed).
"""


def _ensure_git_ident(h: Path) -> None:
    """Give the harness repo a local commit identity (tt-project <tt-project@localhost>) for any key
    its own .git/config lacks, so a plain `git merge` or `git commit` in it works for any run, even
    where no global identity is set. A locally configured name or email is never overwritten."""
    for key, val in (("user.name", "tt-project"), ("user.email", "tt-project@localhost")):
        if subprocess.run(["git", "-C", str(h), "config", "--local", key], capture_output=True,
                          text=True).stdout.strip() == "":
            _git(h, "config", "--local", key, val)


def _emptied(h: Path, have: str | None, ref: str) -> list[str]:
    """Template files (runtime, prompts, bin) that `have` (a commit, or None for the files on disk)
    holds empty while `ref` has them with content. A crash or reboot right after a write leaves
    exactly this, and the merge would then keep the empty file as if it were a local edit."""
    def sizes(rev: str) -> dict[str, int]:
        out = {}
        for entry in _git(h, "ls-tree", "-r", "-l", "-z", rev, "--", "runtime", "prompts", "bin").split("\0"):
            meta, _, path = entry.partition("\t")
            if path and meta.split()[-1].isdigit():
                out[path] = int(meta.split()[-1])
        return out
    full = {f for f, n in sizes(ref).items() if n}
    if have is None:
        return sorted(f for f in full if (h / f).is_file() and not (h / f).is_symlink()
                      and (h / f).stat().st_size == 0)
    return sorted(f for f, n in sizes(have).items() if n == 0 and f in full)


def _blob(h: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(h), "show", f"{rev}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _top_names(src: bytes) -> set[str] | None:
    """Top-level names a Python file defines, or None when it does not parse."""
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return None
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _why_cut(f: str, got: bytes | None, want: bytes, keep: set[str] | None = None) -> str:
    """How `got` lost the runtime file `want` ("" when it did not): deleted, emptied, no longer parsing,
    or missing top-level names `want` defines (only those in `keep`, when given)."""
    if got is None:
        return "deleted"
    if not got.strip():
        return "empty"
    if not f.endswith(".py") or (need := _top_names(want)) is None:
        return ""
    names = _top_names(got)
    if names is None:
        return "cut short"
    lost = need & (need if keep is None else keep) - names
    return "lost " + ", ".join(sorted(lost)[:5]) if lost else ""


def _cut_runtime(h: Path, have: str | None, ref: str) -> dict[str, str]:
    """runtime/ files `ref` ships with content that `have` (a commit, or None for the files on disk)
    lost, mapped to how (see _why_cut; on disk only names HEAD defines too count). A reboot partway
    through an upgrade leaves this; committing it as a local change breaks `import ttp.daemon`
    (e.g. `cannot import name 'poll_s'`)."""
    diff = ["diff", "--name-only", "--no-renames"] + ([have, ref] if have else ["HEAD"]) + ["--", "runtime"]
    cut = {}
    for f in _git(h, *diff).splitlines():
        want = _blob(h, ref, f)
        if not want or not want.strip():
            continue        # not in `ref`, or empty there: nothing to lose
        if have is None:
            p = h / f
            got = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            why = _why_cut(f, got, want, _top_names(_blob(h, "HEAD", f) or b""))
        else:
            why = _why_cut(f, _blob(h, have, f), want)
        if why:
            cut[f] = why
    return cut


def _cut_list(cut: dict[str, str]) -> str:
    return ", ".join(f"{f} ({why})" for f, why in sorted(cut.items()))


def _restore_cut_runtime(h: Path) -> None:
    """Before the upgrade commits local changes: put back runtime/ files that upstream still ships but
    are deleted or cut short on disk, from HEAD (or upstream when HEAD's copy is cut too). Refuses the
    upgrade, with nothing committed, when they cannot be put back."""
    cut = _cut_runtime(h, None, "upstream")
    if not cut:
        return
    for f in cut:
        src = "upstream" if _why_cut(f, _blob(h, "HEAD", f), _blob(h, "upstream", f) or b"") else "HEAD"
        r = subprocess.run(["git", "-C", str(h), "checkout", src, "--", f], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"upgrade refused: {f} is {cut[f]} and could not be restored from {src}: "
                f"{r.stderr.strip()[-300:]}. Nothing was committed; restore it, then rerun `ttp upgrade`.", 1)
    left = _cut_runtime(h, None, "upstream")
    if left:
        die("upgrade refused: runtime files upstream ships are still deleted or cut short after restoring "
            f"them: {_cut_list(left)}. Nothing was committed; restore them, then rerun `ttp upgrade`.", 1)
    print("warning: restored runtime files a crash deleted or cut short instead of committing them: "
          + _cut_list(cut))


def _runtime_problem(tmp: Path) -> str:
    """Why the runtime in worktree `tmp` does not compile or import ("" when it does)."""
    env = {**os.environ, "PYTHONPATH": str(tmp / "runtime")}
    for check in ([sys.executable, "-m", "compileall", "-q", "runtime"],
                  [sys.executable, "-c", "import ttp.daemon, ttp.cli"]):
        c = subprocess.run(check, cwd=str(tmp), env=env, capture_output=True, text=True, timeout=300)
        if c.returncode != 0:
            return f"the merged runtime fails `{' '.join(check[1:])}`: " + (c.stderr or c.stdout).strip()[-400:]
    return ""


def _restore_from_upstream(tmp: Path, ident: list[str], cut: dict[str, str]) -> str:
    """Check out and commit `cut`'s runtime files from upstream in worktree `tmp` ("" when done)."""
    if not cut:
        return ""
    r = subprocess.run(["git", "-C", str(tmp), "checkout", "upstream", "--", *cut], capture_output=True, text=True)
    if r.returncode != 0:
        return (f"runtime files upstream ships are deleted or cut short on main and could not be "
                f"restored: {_cut_list(cut)}: {r.stderr.strip()[-300:]}")
    _git(tmp, *ident, "commit", "-q", "-m", "restore runtime files main lost: " + _cut_list(cut))
    print("warning: restored runtime files main had deleted or cut short: " + _cut_list(cut))
    return ""


LOCAL_OPEN, LOCAL_CLOSE = "<!-- ttp:local -->", "<!-- /ttp:local -->"


def _settle_merge(tmp: Path, ident: list[str], files: list[str]) -> str:
    """Finish the stopped merge of upstream in worktree `tmp` without a model when no conflict needs
    judgment (batch.settle): both sides only added lines at one spot, they changed different lines,
    or upstream already makes the project's change (comments and spacing aside); never when a line
    one side added nearly repeats one the other added (batch._near). A template prompt
    the project only added to takes upstream's text with the project's blocks put back
    (_keep_local_blocks). Commits it and returns ""; else names the files left (nothing is committed)."""
    from .batch import _show, settle
    left, kinds, blocks = [], set(), []
    for f in files:
        if settle(tmp, f, [], taken_in=True, hunks=True, superseded=True, near=True, kinds=kinds):
            continue
        sides = [_show(tmp, n, f) for n in (2, 1, 3)]
        merged = None
        if f.startswith("prompts/") and f.endswith(".md") and None not in sides:
            try:
                merged = _keep_local_blocks(*(s.decode() for s in sides))
            except UnicodeDecodeError:
                pass
        if merged is None:
            left.append(f)
        else:
            durable_write(tmp / f, merged)
            blocks.append(f)
    if left or not files:
        return "the merge conflicts in " + ", ".join(left)
    _git(tmp, "add", "--", *files)
    _git(tmp, *ident, "commit", "-q", "--no-edit", "--no-verify")
    print("settled the template merge's conflicts, which needed no judgment: " + ", ".join(files))
    if "superseded" in kinds:
        print("took upstream's version where it already makes the project's change; the project's own "
              "wording stays in the merge's first parent")
    if blocks:
        print(f"kept this project's own blocks in upstream's text, fenced as {LOCAL_OPEN}: " + ", ".join(blocks))
    return ""


def _keep_local_blocks(ours: str, base: str, theirs: str) -> str | None:
    """A template prompt merged as upstream's text (`theirs`) plus this project's own blocks, when the
    project only added to it: its text outside `<!-- ttp:local -->` fences is `base` with lines inserted.
    Each block goes back after the base line it followed (after upstream's rewrite of that spot, if
    any); added lines not yet fenced get the fence lines, so the next upgrade finds them as the
    project's (the prompts leave the fence lines out). None when the project changed or removed
    template text, a fence is not closed, or a line the project added nearly repeats one upstream
    added or changed at the same spot (batch._near: two versions of one line): that needs judgment."""
    import difflib

    from .batch import _near
    items: list[tuple[str, object]] = []       # ("t", template line) or ("b", fenced block lines)
    block = None
    for line in ours.splitlines(keepends=True):
        s = line.strip()
        opens, closes = (s.startswith(m[:-4]) and s.endswith("-->") for m in (LOCAL_OPEN, LOCAL_CLOSE))
        if block is not None:
            block.append(line)
            if closes:
                items.append(("b", block))
                block = None
        elif opens:
            block = [line]
        elif closes:
            return None
        else:
            items.append(("t", line))
    if block is not None:
        return None
    old = base.splitlines(keepends=True)
    tmpl = [x for kind, x in items if kind == "t"]
    added = [False] * len(tmpl)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, tmpl, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            return None
        if tag == "insert":
            added[j1:j2] = [True] * (j2 - j1)
    groups: list[tuple[int, list[str]]] = []    # (base lines before it, the block's lines)
    at, j, new = 0, 0, []

    def put(lines: list[str]) -> None:
        if groups and groups[-1][0] == at:
            groups[-1][1].extend(lines)
        else:
            groups.append((at, list(lines)))

    def fence() -> None:
        if new:
            body = [x if x.endswith("\n") else x + "\n" for x in new]
            put([LOCAL_OPEN + "\n", *body, LOCAL_CLOSE + "\n"])
            new.clear()
    for kind, x in items:
        if kind == "b":
            fence()
            put([y if y.endswith("\n") else y + "\n" for y in x])
        elif added[j]:
            new.append(x)
            j += 1
        else:
            fence()
            at, j = at + 1, j + 1
    fence()
    if not groups:
        return None
    out = theirs.splitlines(keepends=True)
    ops = difflib.SequenceMatcher(None, old, out, autojunk=False).get_opcodes()
    for i, lines in groups:                     # only upstream's lines added or changed at the block's spot
        mine = [x for x in lines if x.strip() not in (LOCAL_OPEN, LOCAL_CLOSE)]
        if _near(mine, [x for tag, i1, i2, j1, j2 in ops if tag != "equal" and i1 <= i <= i2
                        for x in out[j1:j2]], False):
            return None

    def where(i: int) -> int:
        for tag, i1, i2, j1, j2 in ops:
            if tag == "equal" and i1 <= i <= i2:
                return j1 + i - i1
            if tag != "equal" and i == i1:
                return j1
            if tag != "equal" and i1 < i <= i2:
                return j2
        return len(out)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for i, _, lines in sorted(((where(i), k, lines) for k, (i, lines) in enumerate(groups)), reverse=True):
        out[i:i] = lines
    return "".join(out)


def _merge_upstream(h: Path, tmp: Path, ident: list[str], base: str = "main",
                    kept: dict[str, str] | None = None) -> tuple[str, str]:
    """Merge `upstream` into a scratch worktree of main and check the result compiles and imports.
    Returns (merge commit, "") or ("", what went wrong); the live harness is never touched here.
    Runtime files kept although they lack names upstream ships are added to `kept` (file -> how)."""
    subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    subprocess.run(["git", "-C", str(h), "worktree", "prune"], capture_output=True)
    _git(h, "worktree", "add", "-q", "--detach", str(tmp), base)
    try:
        r = subprocess.run(["git", "-C", str(tmp), *ident, "merge", "--no-edit", "upstream"], capture_output=True,
                           text=True)
        if r.returncode != 0:
            files = subprocess.run(["git", "-C", str(tmp), "diff", "--name-only", "--diff-filter=U"],
                                   capture_output=True, text=True).stdout.split()
            why = _settle_merge(tmp, ident, files)
            if why:
                return "", (why if files else "the merge failed: " + (r.stderr or r.stdout).strip()[-400:])
        emptied = _emptied(tmp, "HEAD", "upstream")
        if emptied:     # committed as "local changes" after a crash emptied them; nobody empties these on purpose
            _git(tmp, "checkout", "upstream", "--", *emptied)
            _git(tmp, *ident, "commit", "-q", "-m", "restore template files a crash left empty: " + ", ".join(emptied))
        cut = _cut_runtime(tmp, "HEAD", "upstream")
        # Deleted, empty or unparseable files are damage. A file that only lacks top-level names upstream
        # ships may be a deliberate local edit (a helper removed on purpose): it is put back only when
        # the merged runtime does not compile or import without it, so its other local edits survive.
        lost = {f: why for f, why in cut.items() if why.startswith("lost ")}
        why = _restore_from_upstream(tmp, ident, {f: w for f, w in cut.items() if f not in lost})
        if why:
            return "", why
        problem = _runtime_problem(tmp)
        if problem and lost:
            why = _restore_from_upstream(tmp, ident, lost)
            if why:
                return "", why
            lost, problem = {}, _runtime_problem(tmp)
        if problem:
            return "", problem
        if lost:
            print("warning: kept runtime files main changed although they lack names upstream ships (the merged "
                  "runtime compiles and imports without them): " + _cut_list(lost))
            if kept is not None:
                kept.update(lost)
        return _git(tmp, "rev-parse", "HEAD"), ""
    finally:
        subprocess.run(["git", "-C", str(h), "worktree", "remove", "--force", str(tmp)], capture_output=True)


def cmd_alerts(a) -> None:
    if (a.ack is not None) != (a.why is not None) or a.why is not None and not a.why.strip():
        die("ttp alerts: --ack <message id> requires a non-empty --why <text>")
    p = need(a.name, sys.argv[1:])
    if a.ack is not None:
        from . import alerts
        try:
            ep = alerts.ack(p.db, a.ack, a.why)
        except ValueError as e:
            die(f"ttp alerts: {e}")
        from .daemon import release_claims
        release_claims(p, ep)   # closed now: a machine-wide claim goes as it would at the sweep
        print(json.dumps(ep) if a.json else f"Acknowledged alert #{a.ack}: " + (
            ep["cleared_why"] if ep["cleared"] else f"{ep['acked']['why']} (kept in the feed until it clears)"))
        return
    from .notifier import alerts_since
    rows = alerts_since(p, a.after, a.floor)
    if a.json:
        print(json.dumps(rows))
    else:
        for r in rows:
            if not r["cleared"]:
                print(f"#{r['id']} [{r['severity']}] {r['text']}")


def cmd_notifier(a) -> None:
    from . import notifier
    if a.action == "install":
        print(notifier.install())
    elif a.action == "run":
        sys.exit(notifier.main())
    elif a.action == "test":
        notifier.show("tt-project", "Test notification: desktop alerts work on this machine.")
        print("sent a test notification")


def cmd_daemon(a) -> None:
    from .daemon import Daemon
    sys.exit(Daemon(a.dir).run())


def cmd_doctor(a) -> None:
    if getattr(a, "live", None):
        # Measures the agent CLI itself, in a scratch project: no project needed, and real spend.
        from . import live
        r = live.row(a.live)
        print(json.dumps(r, indent=1) if a.json else live.format_row(r))
        sys.exit(1 if any(c["status"] == live.FAIL for c in r["checks"].values()) else 0)
    if getattr(a, "scheduler", False):
        # The installed runtime's command-watcher lifecycle, in a scratch project: model-free, $0.
        from . import schedcheck
        rows = schedcheck.run()
        for what, ok, why in rows:
            print(f"{'pass' if ok else 'FAIL'}  {what}" + (f": {why}" if why else ""))
        sys.exit(0 if rows and all(ok for _, ok, _ in rows) else 1)
    if not a.name:
        die("ttp doctor: name a project, --live <provider> or --scheduler")
    p = need(a.name, sys.argv[1:])
    from .providers import all_providers
    print(status_text(p))
    for prov in all_providers():
        if prov.name == "fake":
            continue
        print(f"provider {prov.name}: {'found ' + prov.binary() if prov.available() else 'not installed'}"
              + (f" · account {prov.account()}" if prov.available() and prov.account() else ""))
    gaps = [f"{prov.name} ({prov.write_fence()})" for prov in all_providers()
            if prov.name != "fake" and prov.available() and prov.write_fence()]
    if gaps:
        print("unfenced providers (their workers can write anywhere, project.db included): " + ", ".join(gaps))
    claude_cfg = p.config()["providers"].get("claude", {})
    from .coordinator import name_list
    names = name_list(claude_cfg.get("mcp_servers") or [])
    if names:
        from .providers import get_provider
        _, unknown = get_provider("claude").mcp_servers(names, [str(p.root), str(p.root.resolve())])
        print(f"mcp servers for workers: {', '.join(names)}"
              + (f" · not defined in your Claude config: {', '.join(unknown)} (servers from a plugin "
                 "cannot be listed; add them with `claude mcp add`)" if unknown else "")
              + ("" if claude_cfg.get("worker_isolation") else " · unused: worker_isolation is off, "
                 "so workers load all your servers"))
    sec = load_secrets()
    print(f"jev: {'key saved' if (sec.get('jev') or {}).get('key') else 'no key (rules-only screening)'}"
          f" · enabled in project: {p.config()['jev'].get('enabled')}")
    print(f"slack: {'bot saved' if (sec.get('slack') or {}).get('bot_token') else 'not configured'}"
          f" · enabled in project: {p.config()['notify'].get('slack')}")
    print(web_line(p))
    from .project import config_problems
    for line in config_problems(p.raw_config(), p.root):
        print(f"project.json: {line}")
    from . import push as _push
    checks = _push.check_list((p.config().get("delivery") or {}).get("push_checks"))
    if checks and (p.config().get("delivery") or {}).get("push_branch"):
        try:
            remote, branch = _push.target(p, p.root)
            ref, missing = _push.unmatched_paths(p.root, remote, branch, checks)
        except (ValueError, OSError):
            ref, missing = "", []
        for m in missing:
            print(f"delivery.push_checks: {m} matches no file on {ref}; every push would fail on it")
    if line := checks_env_line(p):
        print(line)
    for c in _push.unexcluded_log_checks(checks):
        print(f"delivery.push_checks: {c!r} also checks committed *.log output, whose captured lines keep "
              f"trailing whitespace; add the pathspec `-- . {_push.LOG_EXCLUDE}`")


class _Version(argparse.Action):
    """`ttp --version`: the source commit is looked up only when asked for (it may run git)."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(f"ttp {__version__} ({source_commit()})")
        parser.exit()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ttp", description="tt-project: long-running, self-driving projects")
    ap.add_argument("--version", action=_Version, nargs=0, help="show the version and source commit, then exit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create a project; ends with its checked web app link (exit 3: the link "
                       "did not answer and the line says what is broken)")
    s.add_argument("name")
    s.add_argument("--dir", help="project root (default: git top level of the current directory)")
    s.add_argument("--host", help="machine to run on (default: this one)")
    s.add_argument("--describe", help="the brief, inline")
    s.add_argument("--describe-file", help="the brief, from a file ('-' for stdin)")
    s.add_argument("--provider", choices=["claude", "codex", "cursor", "fake"])
    s.add_argument("--no-service", action="store_true", help="do not install a boot-time service")
    s.add_argument("--no-secrets", action="store_true", help="with --host: do not copy your saved keys there")
    s.add_argument("--home-tz", help=argparse.SUPPRESS)     # with --host there: the workstation's zone
    s.add_argument("--home-from", help=argparse.SUPPRESS)   # and its name
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("web", help="web app link (for a remote project: tunnel command and link)")
    s.add_argument("name")
    s.add_argument("--tunnel", action="store_true", help="open the local forward now (no need to ask: it only "
                   "lets this machine view the web app)")
    s.add_argument("--keep", action="store_true", help="with --tunnel: keep it up as a user service across drops "
                   "and reboots (adopts or replaces an existing com.tt-project.tunnel.<name> service)")
    s.add_argument("--unkeep", action="store_true", help="stop and remove the kept tunnel")
    s.set_defaults(fn=cmd_web)
    for name, fn, hlp in (("connect", cmd_connect, "attach this chat to a project; ends with its checked web app link "
                           "(exit 3: the link did not answer and the line says what is broken)"),
                          ("status", cmd_status, "one-screen status"),
                          ("logs", cmd_logs, "daemon log tail"), ("doctor", cmd_doctor, "diagnose setup"),
                          ("prune", cmd_prune, "tidy finished tasks' worktrees now (branches are kept)")):
        s = sub.add_parser(name, help=hlp)
        # `ttp status` alone, inside a project's folder (or a run), is that project's status.
        s.add_argument("name", nargs="?" if name in ("status", "doctor") else None)
        if name == "connect":
            s.add_argument("--chat")
            s.add_argument("--label")
            s.add_argument("--home-tz", help=argparse.SUPPRESS)     # the connecting workstation's zone
            s.add_argument("--home-from", help=argparse.SUPPRESS)   # and its name
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "prune":
            s.add_argument("--dry-run", action="store_true", help="change nothing; list what a sweep would do")
        if name == "logs":
            s.add_argument("--bytes", type=int, default=6000)
        if name == "doctor":
            s.add_argument("--live", choices=["claude", "codex", "cursor"], metavar="PROVIDER",
                           help="measure one agent CLI with real runs in a scratch project (spends up to "
                           "about $0.55; a missing or logged-out CLI is reported, exit 0)")
            s.add_argument("--json", action="store_true", help="with --live: the row as JSON")
            s.add_argument("--scheduler", action="store_true",
                           help="check the command-watcher scheduler's lifecycle in a scratch project, each tick in "
                           "a fresh process (no model, $0; exit 1 on a failed step)")
        s.set_defaults(fn=fn)

    s = sub.add_parser("say", help="send a message to the coordinator")
    s.add_argument("name")
    s.add_argument("text", help="the user's words, verbatim, or '-' for stdin")
    s.add_argument("--chat")
    s.add_argument("--context", default="", help="a relayer's own context (data, never the user's word); "
                   "kept apart from the user's words")
    s.add_argument("--client-id", help=argparse.SUPPRESS)   # set by a forwarding machine; resends are skipped
    s.set_defaults(fn=cmd_say)

    s = sub.add_parser("listen", help="print messages for a chat as they arrive", description=(
        "Print messages for a chat as they arrive. For a project on another machine the listen runs "
        "there over ssh; a dropped connection (ssh exit 255, or a signal ending the session) is "
        "retried after 5 s, doubling to 2 min, and each message is printed once per listener. "
        "Exit codes: 0 messages shown (--once) or --timeout reached; 2 the project refused it "
        "(unknown chat or project); 3 a newer listener for the chat replaced this one (not retried); "
        "75 the project stayed unreachable until --timeout ran out; "
        "any other code is the project's own ttp failing."))
    s.add_argument("name")
    s.add_argument("--chat", required=True)
    s.add_argument("--once", action="store_true", help="exit after the first batch")
    s.add_argument("--timeout", type=float, default=0,
                   help="stop after this many seconds (0: never); also bounds reconnecting")
    s.add_argument("--ack", type=int, metavar="ID",
                   help="mark messages up to ID read; later ones stay unread until acknowledged")
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("note", help="(inside a run) append a progress note, or send one to another project")
    s.add_argument("text")
    s.add_argument("--to", metavar="PROJECT",
                   help="also file the note in the inbox of another project, as this worker's")
    s.add_argument("--severity", choices=["low", "normal", "high"], default="normal",
                   help="with --to: the severity of the event the other project's coordinator gets")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("notify", help="(inside a run) tell the user something; sent when the run ends")
    s.add_argument("text")
    s.add_argument("--severity", choices=["low", "normal"], default="normal",
                   help="high and critical stay the coordinator's")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("upstream", help="upstream notes across machines: receive, forwarding status and targets")
    s.add_argument("--receive", action="store_true", help="file notes sent over ssh (JSON lines on stdin); prints an ack")
    s.add_argument("--via", metavar="ALIAS", help="with --receive: the sending machine's alias")
    s.add_argument("--forward-status", action="store_true", help="per target: cursor, last ok, last error (the default)")
    s.add_argument("--forward-to", metavar="ALIASES",
                   help="machines to send this machine's notes on to (comma-separated), 'default' or 'none'")
    s.set_defaults(fn=cmd_upstream)

    s = sub.add_parser("push", help="guarded push of this worktree to delivery.push_branch")
    s.add_argument("--free", action="store_true",
                   help="push nothing: exit 0 when no other push to the target branch is running, 1 while one is")
    s.add_argument("--detach", action="store_true",
                   help="push in a process of its own; print its marker and a --result probe, and return at once")
    s.add_argument("--result", metavar="MARKER",
                   help="push nothing: report a detached push; exit 0 once it finished (or died), 1 while it runs")
    s.add_argument("--own", action="store_true",
                   help="publish the checked-out branch under its name, as it is (checks, no rebase): this task's "
                        "ttp/t<id>-... branch, or another named branch as a fast-forward only; "
                        "never delivery.push_branch, main or the default branch")
    s.add_argument("--queue", action="store_true",
                   help="push nothing: list the push queue's entries and its last 10 batches")
    s.add_argument("--marker", help=argparse.SUPPRESS)   # the detached process itself
    s.add_argument("--batch", metavar="MARKER", help=argparse.SUPPRESS)   # the push queue's batch process (batch.py)
    s.set_defaults(fn=cmd_push)

    from . import checkplan
    s = sub.add_parser("check-plan", help="run committed coverage independently of candidate publication identity")
    s.add_argument("--definition-commit", required=True, help="full commit ID of the independently reviewed definition")
    s.add_argument("--definition-path", required=True, help="exact repository path of its JSON file")
    s.add_argument("--purpose", required=True, choices=["harness", "candidate"])
    s.set_defaults(fn=checkplan.command)

    s = sub.add_parser("checks", help="(inside a run) run the local checks on HEAD and record the result")
    s.add_argument("--gap", metavar="SIG", help="read-only probe: exit 0 when a missing check's config changed")
    s.add_argument("--gap-config", metavar="FINGERPRINT",
                   help="with --gap: the push_checks fingerprint the waiter failed under")
    s.add_argument("--fresh", action="store_true",
                   help="run the checks even when they already passed on this tree")
    s.add_argument("--purpose", choices=["candidate", "harness"], default="candidate",
                   help="harness: qualify a harness-preparation head with the purpose harness and untagged checks; "
                        "candidate gates are not run and the pass never counts for a PR or a push")
    s.add_argument("--detach", action="store_true",
                   help="run them in a process of their own; print a retry_when that exits 0 once they finished or died")
    s.add_argument("--result", metavar="RUN_DIR",
                   help="run nothing: exit 0 once a run's detached checks finished or died, 1 while they run")
    s.add_argument("--rc", help=argparse.SUPPRESS)   # the detached process itself: where it writes its exit code
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="extra check commands after --: one quoted string runs through the shell "
                        "(ttp checks -- 'FOO=1 pytest -q && ruff check'); several quoted strings run as "
                        "one check each (-- 'pytest -q' 'ruff check .'); plain words run as argv "
                        "(-- pytest -q tests/a.py). One that delivery.push_checks already runs is dropped")
    s.set_defaults(fn=cmd_checks)

    s = sub.add_parser("stats", help="context re-read (cache-read) tokens per run and per $, by kind and tier")
    s.add_argument("name", nargs="?")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("audit", help="the daily review's self-efficiency audit: asks, wasted runs, idle wakes, "
                                     "$ per outcome, review loops, stuck work and overrides held past their end")
    s.add_argument("name", nargs="?")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("spend-today", help="this machine's tt-project spend today, by provider and account "
                                           "(for the global daily cap)")
    s.add_argument("--since", type=float, help="from this Unix time (default: the budget day's start)")
    s.add_argument("--until", type=float)
    s.add_argument("--json", action="store_true")
    s.add_argument("--receive", action="store_true",
                   help="keep the spend another machine pushes on stdin (it runs this over ssh)")
    s.add_argument("--via", help="with --receive: the sending machine's alias")
    s.set_defaults(fn=cmd_spend_today)

    s = sub.add_parser("clip", help="run a command, keep its full output in a file and print a short "
                                    "excerpt (a test run's failures) plus the path")
    s.add_argument("--lines", type=int, default=40, help="at most this many lines of an ordinary excerpt")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, after --")
    s.set_defaults(fn=cmd_clip)

    s = sub.add_parser("killscan", help="flag kills by name or pattern (pkill, killall, pgrep/pidof, ps | grep into kill) "
                                        "in scripts before running them; --shim writes stand-ins that only log")
    s.add_argument("files", nargs="*", help="scripts to check; exits 1 if one kills by name or pattern, 2 if one cannot be read")
    s.add_argument("--shim", metavar="DIR", help="write logging stand-ins for pkill, killall, pgrep and pidof "
                                                 "into DIR (put DIR first on PATH)")
    s.set_defaults(fn=cmd_killscan)

    s = sub.add_parser("lock", help="(inside a run) hold a shared resource while one command runs")
    s.add_argument("--probe", action="store_true",
                   help="exit 0 if the resource is free, not paused and nobody queues for it, else 75 (for retry_when)")
    s.add_argument("resource")
    s.add_argument("--timeout", type=float, default=None,
                   help="give up after this many seconds (exit 75); 0 waits as long as it takes; "
                        "inside a run: at most half its stall limit")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_lock)

    s = sub.add_parser("landed", help="exit 0 once a commit or a task's landing is on the push branch, even "
                                      "rebased (same patch-id or author/date/subject), 1 while not yet, 2 if it "
                                      "cannot tell (for start_when; `landed:#<id>` runs it)")
    s.add_argument("ref", nargs="?", help="a commit, or #<id> for a task's landing")
    s.add_argument("--task", type=int, help="the task whose landing to look for")
    s.add_argument("--onto", help="the branch to look on (default: delivery.push_branch)")
    s.set_defaults(fn=cmd_landed)

    s = sub.add_parser("detach", help="(inside a run) start a job that outlives the run; writes <name>.rc")
    s.add_argument("--check", nargs="*", metavar="RC",
                   help="exit 0 once every job of these .rc paths ended or is gone, else 1 (for retry_when)")
    s.add_argument("--remote", metavar="HOST",
                   help="start the job on this ssh host with setsid nohup; it survives this run and a reboot here")
    s.add_argument("--dir", help="with --remote: the remote folder for its files (default ~/.ttp-detach/<run>)")
    s.add_argument("--host", help="with --check: the ssh host the jobs run on; the paths are <dir>/<name>; "
                   "255 when ssh fails")
    s.add_argument("--cores", type=int, help="cores the job is meant to keep busy (default: read from -j N, "
                   "--jobs/--threads/--workers N, OMP_NUM_THREADS); the daemon flags sustained under-use")
    s.add_argument("--devices", type=int, help="devices the job is meant to use")
    s.add_argument("--mem-gb", type=float, help="memory the job asked for (recorded only)")
    s.add_argument("name", nargs="?")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_detach)

    from . import ciwait
    s = sub.add_parser("ci", help="exit 0 once a commit's CI runs completed or a job hung, 1 while they run, "
                                  "75 if gh cannot answer (for retry_when)")
    s.add_argument("--repo", help="owner/repo (default: the repository of the current directory)")
    s.add_argument("--branch", help="the branch whose newest commit's runs to wait for")
    s.add_argument("--sha", help="wait for this commit's runs instead of the branch's newest")
    s.add_argument("--workflow", help="only this workflow")
    s.add_argument("--run", help="wait for this one run id")
    s.add_argument("--no-hang", action="store_true", help="wait for completion only, never wake on a hung job")
    s.add_argument("--factor", type=float, default=ciwait.FACTOR, help="a job is hung past this many times its median")
    s.add_argument("--floor-min", type=float, default=ciwait.FLOOR_MIN, help="never call a job hung before this many minutes")
    s.add_argument("--default-min", type=float, default=ciwait.DEFAULT_MIN,
                   help="the limit in minutes when no finished run of the workflow is known")
    s.set_defaults(fn=cmd_ci)

    s = sub.add_parser("devq", help="queue jobs on the project's serial device-job runner (config device.runners)")
    s.add_argument("op", choices=["submit", "probe", "start", "status", "clear", "list"])
    s.add_argument("runner", nargs="?")
    s.add_argument("rest", nargs=argparse.REMAINDER,
                   help="submit: --id <unique id> [--config <drop-rule key; default the id>] [--timeout <s>] "
                        "[--workdir <dir on the host>] [--no-lint] -- <command>; probe: <id>; status: [<id>]; clear: <config>")
    s.set_defaults(fn=cmd_devq)

    s = sub.add_parser("list", help="projects registered on this machine")
    s.add_argument("--status", action="store_true", help="one status line per project, as `ttp overview`")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("overview", help="every project on this machine: daemon, version, asks, waiting, running, "
                                        "spend today; other projects are only read")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_overview)
    s = sub.add_parser("deployed", help="exit 0 once every project on this machine runs a release by its daemon's "
                                        "own receipt, 1 while any is pending (with owner and resume time), 2 if "
                                        "it cannot tell; read only (for retry_when)")
    s.add_argument("version", nargs="?", help="the release to look for (default: the installed one)")
    s.add_argument("--commit", help="its commit (default: the installed one's, when the versions match)")
    s.add_argument("--projects", help="only these projects, comma-separated (default: every one `ttp list` shows here)")
    s.add_argument("--fresh-s", type=float, default=300, help="a completed tick at most this old (default 300)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--after-push", action="store_true",
                   help="accept owned merges with takeover times, record follow-ups in this project, "
                        "and alert on failures; exit 0 for done/pending, 1 for failed")
    s.set_defaults(fn=cmd_deployed)
    s = sub.add_parser("find", help="locate a project by name (registry, then chat logs)")
    s.add_argument("name")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("adopt", help="record where a project lives")
    s.add_argument("name")
    s.add_argument("--host")
    s.add_argument("--dir", required=True)
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("task", help="add (deferred with --start-when/--start-after)/list/cancel/requeue tasks "
                                       "by hand (requeue: a blocked or stopped task runs again), re-point a not-yet-started task's probe, "
                                       "or record a done task's work as re-landed under another commit")
    s.add_argument("name")
    s.add_argument("action", choices=["add", "list", "cancel", "requeue", "set-when", "superseded"])
    s.add_argument("title", nargs="?", default="", help="add: the title; cancel/requeue/set-when/superseded: the task id")
    s.add_argument("probe", nargs="?", help="set-when: the new retry_when/start_when command (\"\" clears it); "
                   "superseded: the commit on the push branch that carries the task's work")
    s.add_argument("--spec")
    s.add_argument("--kind", default="work")
    s.add_argument("--tier", default="standard")
    s.add_argument("--priority", type=int, default=3)
    s.add_argument("--start-when", help="add: a probe; the task starts only once it exits 0 (e.g. landed:#<id>)")
    s.add_argument("--start-after", help="add: a delay (2h, 3d) or ISO time before which it does not start")
    s.add_argument("--why", help="add: plain words for --start-when, shown instead of the probe")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("obligation", help="urgent deliverables owed to the user: add, attach owner tasks, "
                                          "record progress and receipts, drop one recorded by mistake, list")
    s.add_argument("action", choices=["add", "attach", "progress", "receipt", "drop", "list"])
    s.add_argument("args", nargs="*", help="add: the text; attach: <id> <task>...; progress/receipt: <id> <evidence>; "
                                           "drop: <id> <why>")
    s.add_argument("--owner", type=int, action="append", help="add: a required owner task (repeatable)")
    s.add_argument("--milestone", help="receipt: the milestone key; leaves the obligation open")
    s.add_argument("--task", type=int, help="progress/receipt outside a run: the owner task it is for")
    s.add_argument("--project", help="the project (default: the one here)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_obligation)

    s = sub.add_parser("memory", help="add a memory, or retire one with --forget")
    s.add_argument("name")
    s.add_argument("text", nargs="?")
    s.add_argument("--kind", default="fact")
    s.add_argument("--forget", action="append", metavar="ENTRY",
                   help="retire an entry (its name in [brackets]) to memory/archive/; repeatable")
    s.add_argument("--standing", action="store_true",
                   help="an instruction that applies every time its case comes up (kept as a preference); "
                        "one finished instance never retires it")
    s.add_argument("--why", help="with --forget: why it ends; required for a standing entry (its end condition "
                                  "or the user's words ending it)")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("schedules", help="list or show schedules (read-only); --export moves them into the harness")
    s.add_argument("name")
    s.add_argument("action", nargs="?", choices=["list", "show"], default="list",
                   help="list (default) or show <schedule>: read-only, writes nothing")
    s.add_argument("schedule", nargs="?", help="the schedule to show")
    s.add_argument("--export", action="store_true",
                   help="MOVES the schedules into harness/schedules.json and COMMITS it; that file then holds "
                        "them. Asks first; needs --yes when not interactive. To only read, use show")
    s.add_argument("--yes", action="store_true", help="confirm --export without a prompt")
    s.set_defaults(fn=cmd_schedules)

    s = sub.add_parser("heal", help="heal checks: list them, or test one's check (dry run)")
    s.add_argument("action", choices=("list", "test"))
    s.add_argument("check", nargs="?", help="test: the heal check (its schedule's name)")
    s.add_argument("--project", help="the project (default: the one this folder or run belongs to)")
    s.set_defaults(fn=cmd_heal)

    s = sub.add_parser("machines", help="your machines, shared by all your projects (add/list/status/remove/push)")
    ms = s.add_subparsers(dest="action", required=True)
    m = ms.add_parser("add", aliases=["set"], help="add a machine, or change its tags, note or recovery owner")
    m.add_argument("alias", help="a short name, also used as the resource name in tasks (e.g. box-a)")
    m.add_argument("--tags", help="what it offers, comma-separated (e.g. device,x86)")
    m.add_argument("--note", help="one line for the coordinator (no secrets)")
    m.add_argument("--until", help="when the note stops being true (a delay such as 3d, or an ISO time); past "
                                   "it the coordinator sees it as stale (\"\" = no end)")
    m.add_argument("--min-free-gb", dest="min_free_gb",
                   help="disk guard threshold on this machine's filesystem, overriding the projects' "
                        "disk.min_free_gb there (0 = off; \"\" = back to the projects' own)")
    m.add_argument("--hostname", help="its short host name, when that is not the alias (\"\" = none)")
    m.add_argument("--shared", nargs="?", const="", metavar="NAMES",
                   help="resources on it that all your projects share: one set of `ttp lock` slots and one "
                        "pause across projects (comma-separated; default: the alias itself)")
    m.add_argument("--unshared", action="store_true", help="its resources are per project again")
    m.add_argument("--owner", help="the project that recovers it when it stays down or held (\"\" = none)")
    m.add_argument("--fallback", help="the project that takes over when the owner cannot act (\"\" = none)")
    m = ms.add_parser("list", help="list your machines")
    m.add_argument("--json", action="store_true")
    m = ms.add_parser("status", help="per machine: the projects using it, its recovery owner, pauses, open changes, "
                                     "heal checks and problems in 24 h (read only)")
    m.add_argument("alias", nargs="?", help="only this machine")
    m.add_argument("--json", action="store_true")
    m = ms.add_parser("remove", help="remove a machine")
    m.add_argument("alias")
    m = ms.add_parser("push", help="copy the list to the machines your --host projects run on, merged")
    m.add_argument("--host", help="only this machine")
    m = ms.add_parser("outage", help="a known outage (hardware repair, an external ticket): nobody is paged "
                                     "about the machine until it ends; the daily review lists it")
    m.add_argument("alias", nargs="?", help="the machine (none: list the known outages)")
    m.add_argument("why", nargs="?", help="why it is out, one line (none: show it)")
    m.add_argument("--owner", help="who has the repair: a person's role or a ticket, not a project")
    m.add_argument("--expires", help="when it ends: a delay (3d) or a time (required to record one)")
    m.add_argument("--by", help="the project recording it (default: the project you are in)")
    m.add_argument("--clear", action="store_true", help="the outage is over")
    m = ms.add_parser("change", help="the ledger of live changes to shared machines, seen by every co-tenant")
    cs = m.add_subparsers(dest="change", required=True)
    c = cs.add_parser("add", help="record a live change: what, how to undo it and when it ends")
    c.add_argument("alias")
    c.add_argument("what", help="what changed and why, one line")
    c.add_argument("--undo", help="the command that undoes it (required, except for --power-cycle-at)")
    end = c.add_mutually_exclusive_group()
    end.add_argument("--expires", help="when it ends: a delay (6h, 3d) or a time")
    end.add_argument("--until-probe", dest="until_probe",
                     help="a command that exits 0 once the change should be undone (checked every 10 min)")
    end.add_argument("--power-cycle-at", dest="power_cycle_at", metavar="T",
                     help="a planned power cycle at T (a delay or a time): running workers on the machine are "
                          "told to commit and hand off shortly before; the entry closes itself after")
    c.add_argument("--by", help="the project that owns the change (default: the project you are in)")
    c.add_argument("--expect", action="append", metavar="FINGERPRINT|CONDITION",
                   help="an alert the change is known to cause (an event fingerprint, or down/held on this "
                        "machine): it keeps the coordinator's turn routine while the change is open; repeatable")
    c = cs.add_parser("list", help="the open changes")
    c.add_argument("alias", nargs="?")
    c.add_argument("--all", action="store_true", help="closed ones too")
    c.add_argument("--json", action="store_true")
    c = cs.add_parser("close", help="the change was undone")
    c.add_argument("id", type=int)
    c.add_argument("--why")
    c.add_argument("--by")
    s.set_defaults(fn=cmd_machines)

    for name in ("pause", "resume"):
        s = sub.add_parser(name, help=f"{name} the project, or with --resource one shared resource")
        s.add_argument("name")
        s.add_argument("--resource", help=f"{name} only this resource: tasks using it wait, `ttp lock` refuses it"
                       if name == "pause" else f"{name} only this resource")
        if name == "pause":
            s.add_argument("--reason", help="why, shown to workers, the coordinator and in status")
            s.add_argument("--until", help="with --resource: when it should end, a delay (2d) or an ISO time, at "
                                           "most 7 days ahead; this or --end-when is required")
            s.add_argument("--end-when", dest="end_when", metavar="PROBE",
                           help="with --resource: a read-only shell probe that exits 0 once the pause can end")
            s.add_argument("--report-from", dest="report_from", metavar="PROJECT",
                           help="with --resource: the project whose report it waits on; its notes re-prompt the coordinator")
            s.add_argument("--until-reboot", dest="until_reboot", action="store_true",
                           help="the whole project until this host reboots: the daemon lifts it on its first tick "
                                "on a new boot")
        s.set_defaults(fn=cmd_pause)
    s = sub.add_parser("drain", help="pause the project and wait until its runs in flight end (they are not killed)")
    s.add_argument("name")
    s.add_argument("--wait", type=float, default=0, metavar="S",
                   help="seconds to wait; exits 0 once no run or push is in flight, 1 at the timeout (default 0: check once)")
    s.add_argument("--jobs", action="store_true",
                   help="also wait for the project's `ttp detach` jobs running on this host (listed either way)")
    s.add_argument("--reboot", metavar="REASON",
                   help="record a planned reboot or power cycle of this host, about to happen: on the next boot "
                        "the detached jobs it ends do not count as the tasks' reboot losses")
    s.set_defaults(fn=cmd_drain)
    for name in ("start", "stop", "restart"):
        s = sub.add_parser(name, help=f"{name} the project's daemon service (running workers are kept)")
        s.add_argument("name")
        if name == "stop":
            s.add_argument("--kill", action="store_true", help="also end running workers; their tasks resume on start")
        s.set_defaults(fn=cmd_service)

    s = sub.add_parser("config", help="read or set a config key (dotted); with --account, the account-level "
                                      "value every project on this machine reads: ttp config --account KEY [VALUE]")
    s.add_argument("name")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.add_argument("--account", action="store_true",
                   help="the account-level setting (" + ", ".join(
                       f"{sec}.{k}" for sec, ks in ACCOUNT_KEYS.items() for k in sorted(ks)) + "); '' removes it")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("secret", help="store per-user credentials (read from stdin)")
    s.add_argument("kind", choices=["jev", "slack", "show", "push"])
    s.add_argument("--host", help="with push: the machine to copy your saved keys to")
    s.add_argument("--via", default="typesafe", choices=["typesafe", "openrouter"])
    s.add_argument("--url")
    s.add_argument("--email")
    s.add_argument("--user-id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser("setup", help="install ttp for this user (stable copy + shim on PATH)")
    s.add_argument("--bin-dir", default="~/.local/bin")
    s.add_argument("--force", action="store_true", help="install even over a newer installed ttp")
    s.add_argument("--no-shortcut", action="store_true",
                   help="remove (and do not install) the plain /tt-project command for Claude Code")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("upgrade", help="merge the installed tt-project template into a project's harness")
    s.add_argument("name")
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)          # the daemon's own upgrade
    s.add_argument("--apply", metavar="COMMIT",
                   help="an upgrade task's last step: fast-forward main to its checked merge and restart")
    s.add_argument("--project-dir", help=argparse.SUPPRESS)
    s.add_argument("--take-over-from", metavar="TASK", type=int,
                   help="host-side takeover: harness task TASK missed its takeover deadline, so this upgrade "
                        "takes the template merge over from it even while it still shows activity")
    s.set_defaults(fn=cmd_upgrade)

    s = sub.add_parser("alerts", help="list broadcast alerts or acknowledge a keyed alert")
    s.add_argument("name")
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--floor", default="high")
    s.add_argument("--json", action="store_true")
    s.add_argument("--ack", type=int, metavar="ID", help="acknowledge the keyed alert of this message id: it closes, or moves to the feed while it holds")
    s.add_argument("--why", help="reason for acknowledging the alert (required with --ack)")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("notifier", help="desktop notifications on this machine for all your projects")
    s.add_argument("action", choices=["install", "run", "test"])
    s.set_defaults(fn=cmd_notifier)

    s = sub.add_parser("daemon", help=argparse.SUPPRESS)
    s.add_argument("dir")
    s.set_defaults(fn=cmd_daemon)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
