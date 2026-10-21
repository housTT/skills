"""setup-box agents on a box: `box.py agents check|install|login|editor-defaults`.

- `check <alias>` is one read-only ssh probe: each agent CLI and its version, whether a plain
  `ssh <alias> <cmd>` and a login shell both find it, whether it is logged in, and the Claude Code and
  Codex extensions on every editor server build (Cursor and VS Code) on the box. Each gap comes with
  the command this skill runs to fix it. Exit 0 ready, 1 a gap, 255 ssh failed.
- `install <alias> [--cli ...] [--extensions] --run` installs the CLIs with their official installers
  (idempotent, checked with `--version`), puts ~/.local/bin on PATH for ssh commands and login shells,
  and installs both extensions on every editor server build (checked with `--list-extensions`).
  Without `--run` it prints what it would run.
- `login <alias> <cli>` starts the CLI's own headless login on the box, detached, and prints the
  sign-in link and code for the user. `--code-stdin` passes a code the sign-in page shows back to a
  login that asks for one; `--status` says whether the CLI is logged in. Tokens never leave the box:
  the login's output is shown only up to the sign-in link and code and removed when it ends.
- `editor-defaults [--run]` adds both extensions to `remote.SSH.defaultExtensions` in Cursor's and
  VS Code's user settings on this machine (merged, existing entries kept, the file backed up), so a
  new editor server build on any box gets them. Only on the user's yes: it is their machine.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import platform
import re
import shlex
import sys
from typing import Dict, List, Optional, Sequence

import box

BOX_PY = str(pathlib.Path(__file__).resolve().parent / "box.py")
CLIS: Dict[str, dict] = {
    "claude": {"name": "Claude Code", "install": "curl -fsSL https://claude.ai/install.sh | bash",
               "login": "auth login", "ext": "anthropic.claude-code"},
    "codex": {"name": "Codex", "install": "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
              "login": "login --device-auth", "ext": "openai.chatgpt"},
}
DEFAULT_CLIS = tuple(CLIS)
EXTENSIONS = tuple(c["ext"] for c in CLIS.values())
# Every editor server build a Remote-SSH editor leaves on a box (Cursor's old and new layout, VS Code's).
SERVER_GLOBS = ('"$HOME"/.cursor-server/bin/*/bin/cursor-server',
                '"$HOME"/.cursor-server/bin/*/*/bin/cursor-server',
                '"$HOME"/.vscode-server/bin/*/bin/code-server',
                '"$HOME"/.vscode-server/cli/servers/*/server/bin/code-server')
PATH_MARK = "# tt-project setup-box: agent CLIs on PATH"
PATH_LINE = ('case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac  '
             + PATH_MARK)
LOGIN_DIR = '"$HOME"/.cache/tt-project/agent-login'
SETTING = "remote.SSH.defaultExtensions"


def _clis(arg: Optional[str]) -> List[str]:
    out = [c for c in (arg or ",".join(DEFAULT_CLIS)).split(",") if c and c != "none"]
    bad = [c for c in out if c not in CLIS]
    if bad:
        raise ValueError(f"unknown CLI {', '.join(bad)} (scripted: {', '.join(CLIS)})")
    return out


def _find(cli: str) -> str:
    """Shell: the CLI's path into $b, from PATH or the installers' ~/.local/bin."""
    return (f'b=$(command -v {cli} 2>/dev/null || true); '
            f'[ -z "$b" ] && [ -x "$HOME/.local/bin/{cli}" ] && b="$HOME/.local/bin/{cli}"; ')


def _authed(cli: str) -> str:
    """Shell condition, true when the CLI at $b is logged in; it prints nothing of the status."""
    if cli == "claude":
        return ('$T "$b" auth status --json </dev/null 2>/dev/null | tr -d " " | grep -q \'"loggedIn":true\'')
    return '$T "$b" login status </dev/null >/dev/null 2>&1'


_TIMEOUT = 'T=; command -v timeout >/dev/null 2>&1 && T="timeout 60"; '


def probe_script(clis: Sequence[str], editors: bool = True) -> str:
    """Read-only shell run on the box; prints key=value lines and never prints a token."""
    s = _TIMEOUT
    for c in clis:
        s += (f'echo "ni_{c}=$(command -v {c} 2>/dev/null)"; '
              f'echo "login_{c}=$("${{SHELL:-/bin/sh}}" -lc \'command -v {c}\' </dev/null 2>/dev/null | tail -n 1)"; '
              + _find(c) + f'echo "bin_{c}=$b"; '
              f'if [ -n "$b" ]; then echo "ver_{c}=$($T "$b" --version </dev/null 2>/dev/null | head -n 1)"; '
              f'if {_authed(c)}; then echo auth_{c}=yes; else echo auth_{c}=no; fi; fi; ')
    if editors:
        s += (f'for s in {" ".join(SERVER_GLOBS)}; do [ -x "$s" ] || continue; '
              'echo "server=$s|$($T "$s" --list-extensions </dev/null 2>/dev/null | tr "\\n" ",")"; done; ')
    return s + "true"


def server_label(path: str) -> str:
    """`cursor <build>` or `vscode <build>` from a server binary's path."""
    kind = "cursor" if "/.cursor-server/" in path else "vscode"
    parts = path.split("/")
    i = next((n for n, p in enumerate(parts) if p in (".cursor-server", ".vscode-server")), 0)
    build = [p for p in parts[i + 1:] if p not in ("bin", "cli", "servers", "server", "cursor-server", "code-server")]
    return f"{kind} {'/'.join(build) or '?'}"


def read_check(out: str, alias: str, clis: Sequence[str], exts: Sequence[str]) -> "tuple[List[str], List[str], List[str]]":
    """(report lines, problems, fixes) from the probe's key=value lines."""
    rows = [ln.split("=", 1) for ln in out.splitlines() if "=" in ln]
    kv = {k: v.strip() for k, v in rows}
    lines: List[str] = []
    problems: List[str] = []
    fixes: List[str] = []

    def fix(cmd: str) -> None:
        if cmd not in fixes:
            fixes.append(cmd)
    for c in clis:
        name = CLIS[c]["name"]
        if not kv.get(f"bin_{c}"):
            problems.append(f"{c}: not installed")
            lines.append(f"{c}: not installed")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
            continue
        ver = kv.get(f"ver_{c}") or "?"
        if not kv.get(f"ver_{c}"):
            problems.append(f"{c}: `--version` failed")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
        path_gap = [w for w, k in (("ssh commands", "ni"), ("login shells", "login"))
                    if not kv.get(f"{k}_{c}", "").startswith("/")]
        if path_gap:
            problems.append(f"{c}: not on PATH for {' and '.join(path_gap)}")
            fix(f"{BOX_PY} agents install {alias} --cli {c} --run")
        authed = kv.get(f"auth_{c}") == "yes"
        if not authed:
            problems.append(f"{c}: not logged in")
            fix(f"{BOX_PY} agents login {alias} {c}  (relay its sign-in link and code to the user)")
        lines.append(f"{c}: {ver if name.split()[0].lower() in ver.lower() else f'{name} {ver}'}" + (f", not on PATH for {' and '.join(path_gap)}" if path_gap else "")
                     + (", logged in" if authed else ", not logged in"))
    servers = [v.split("|", 1) for k, v in rows if k == "server"]
    if exts and not servers:
        lines.append("editor servers: none on the box yet (an editor's first Remote-SSH connect installs one; "
                     "with `remote.SSH.defaultExtensions` set it brings both extensions, else run the install "
                     "--extensions fix after that connect)")
    for path, listed in servers:
        have = {e.strip().lower() for e in listed.split(",") if e.strip()}
        miss = [e for e in exts if e.lower() not in have]
        label = server_label(path)
        lines.append(f"editor {label}: " + ("extensions ok" if not miss else f"missing {', '.join(miss)}"))
        if miss:
            problems.append(f"editor {label}: missing {', '.join(miss)}")
            fix(f"{BOX_PY} agents install {alias} --cli none --extensions --run")
    return lines, problems, fixes


def install_script(clis: Sequence[str], exts: Sequence[str]) -> str:
    """Idempotent shell run on the box: CLIs, PATH lines, editor extensions; key=value result lines."""
    s = ("set -u; fail=0; umask 022; mkdir -p \"$HOME/.local/bin\" \"$HOME/.cache/tt-project\"; "
         'T=; TL=; command -v timeout >/dev/null 2>&1 && { T="timeout 60"; TL="timeout 600"; }; ')
    if clis:
        s += (f"LINE={shlex.quote(PATH_LINE)}; MARK={shlex.quote(PATH_MARK)}; "
              'put() { f=$1; if [ -f "$f" ] && grep -qF "$MARK" "$f"; then echo "path=$f already"; return; fi; '
              '[ -f "$f" ] && [ ! -e "$f.ttp-bak" ] && cp -p "$f" "$f.ttp-bak"; t=$(mktemp) || return 1; '
              'if [ "$2" = top ]; then { printf "%s\\n" "$LINE"; cat "$f" 2>/dev/null; } >"$t"; '
              'else { cat "$f" 2>/dev/null; printf "%s\\n" "$LINE"; } >"$t"; fi; '
              'cat "$t" >"$f"; rm -f "$t"; echo "path=$f added"; }; '
              # ~/.bashrc first line: Debian-style ones return early for the shell an ssh command runs in.
              'put "$HOME/.bashrc" top; '
              'if [ -f "$HOME/.bash_profile" ]; then put "$HOME/.bash_profile" end; else put "$HOME/.profile" end; fi; '
              'case "${SHELL:-}" in */zsh) put "$HOME/.zshenv" top ;; esac; '
              'PATH="$HOME/.local/bin:$PATH"; export PATH; ')
    for c in clis:
        log = f'"$HOME/.cache/tt-project/agents-install-{c}.log"'
        s += (_find(c) + f'if [ -n "$b" ] && v=$($T "$b" --version </dev/null 2>/dev/null) && [ -n "$v" ]; then '
              f'echo "cli_{c}=present $v"; else '
              f'( {CLIS[c]["install"]} ) >{log} 2>&1; ' + _find(c)
              + f'if [ -n "$b" ] && v=$($T "$b" --version </dev/null 2>/dev/null) && [ -n "$v" ]; then '
              f'echo "cli_{c}=installed $v"; else echo "cli_{c}=failed"; fail=1; '
              f'tail -n 5 {log} | sed "s/^/log_{c}=/"; fi; fi; ')
    if exts:
        s += (f'n=0; for s in {" ".join(SERVER_GLOBS)}; do [ -x "$s" ] || continue; n=$((n+1)); '
              'have=$($T "$s" --list-extensions </dev/null 2>/dev/null); '
              f'for e in {" ".join(exts)}; do '
              'if printf "%s\\n" "$have" | grep -qix "$e"; then echo "ext=$s|$e|present"; '
              'elif $TL "$s" --install-extension "$e" </dev/null >/dev/null 2>&1 && '
              '$T "$s" --list-extensions </dev/null 2>/dev/null | grep -qix "$e"; then echo "ext=$s|$e|installed"; '
              'else echo "ext=$s|$e|failed"; fail=1; fi; done; done; [ "$n" = 0 ] && echo "ext=none"; ')
    return s + 'exit "$fail"'


def read_install(out: str) -> "tuple[List[str], bool]":
    lines, ok = [], True
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k.startswith("cli_"):
            lines.append(f"{k[4:]}: {v}")
            ok = ok and not v.startswith("failed")
        elif k.startswith("log_"):
            lines.append(f"  {k[4:]} installer: {v}")
        elif k == "path":
            lines.append(f"PATH line: {v}")
        elif k == "ext" and v == "none":
            lines.append("editor servers: none on the box yet (an editor's first Remote-SSH connect installs one)")
        elif k == "ext":
            path, ext, state = (v.split("|") + ["", ""])[:3]
            lines.append(f"editor {server_label(path)}: {ext} {state}")
            ok = ok and state != "failed"
    return lines, ok


def login_script(cli: str) -> str:
    """Start the CLI's headless login on the box, detached, unless it is logged in or one is pending.

    Its stdin is a fifo held open, so a code the sign-in page shows can be passed back
    (`--code-stdin`); output goes to an owner-only log that is removed when the login ends.
    """
    c, args = cli, CLIS[cli]["login"]
    return (_TIMEOUT + f'umask 077; d={LOGIN_DIR}; mkdir -p "$d"; chmod 700 "$d"; ' + _find(c)
            + 'if [ -z "$b" ]; then echo state=no-cli; exit 0; fi; '
            f'if {_authed(c)}; then echo state=logged-in; exit 0; fi; '
            f'pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo state=pending; else '
            f'rm -f "$d/{c}.in" "$d/{c}.log"; mkfifo "$d/{c}.in" || {{ echo state=failed; exit 0; }}; '
            'TL=; command -v timeout >/dev/null 2>&1 && TL="timeout 900"; '
            'nohup setsid sh -c \'exec 3<>"$1"; $2 <&3 >"$3" 2>&1; exec 3<&-; sleep 5; rm -f "$1" "$3" "$4"\' sh '
            f'"$d/{c}.in" "$TL $b {args}" "$d/{c}.log" "$d/{c}.pid" </dev/null >/dev/null 2>&1 & '
            f'echo $! >"$d/{c}.pid"; echo state=started; fi; '
            f'i=0; while [ $i -lt 30 ] && ! grep -q "https://" "$d/{c}.log" 2>/dev/null; do sleep 1; i=$((i+1)); done; '
            f'sed -n "1,40p" "$d/{c}.log" 2>/dev/null | sed "s/^/out=/"; true')


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_TOKENISH = re.compile(r"\b[A-Za-z0-9_\-.]{40,}\b")


def relay_lines(out: str) -> List[str]:
    """The login's own instructions, link and code; anything token-like outside a link is redacted."""
    keep = []
    for ln in out.splitlines():
        if not ln.startswith("out="):
            continue
        t = _ANSI.sub("", ln[4:]).rstrip()
        if not t.strip() or t.lstrip().startswith("WARNING"):
            continue
        if "https://" not in t:
            t = _TOKENISH.sub("<redacted>", t)
        keep.append(t)
    return keep


def code_script(cli: str) -> str:
    """Pass one line from stdin (the code the sign-in page showed) to the pending login's fifo."""
    return (f'd={LOGIN_DIR}; IFS= read -r code || true; '
            f'pid=$(cat "$d/{cli}.pid" 2>/dev/null); '
            'if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null || [ ! -p "$d/' + cli + '.in" ]; then '
            'echo state=no-login; exit 1; fi; '
            f'printf "%s\\n" "$code" >"$d/{cli}.in"; echo state=sent')


def status_script(cli: str) -> str:
    return (_TIMEOUT + f'd={LOGIN_DIR}; ' + _find(cli) + 'if [ -z "$b" ]; then echo state=no-cli; '
            f'elif {_authed(cli)}; then echo state=logged-in; '
            f'elif pid=$(cat "$d/{cli}.pid" 2>/dev/null) && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; '
            'then echo state=pending; else echo state=logged-out; fi')


def _state(out: str) -> str:
    return next((ln.split("=", 1)[1].strip() for ln in out.splitlines() if ln.startswith("state=")), "")


def _ssh(alias: str, script: str, timeout: float, input: Optional[str] = None):
    return box.ssh_run(alias, ["sh", "-c", script], timeout=timeout, input=input)


def _ssh_failed(alias: str, r) -> Optional[int]:
    if r.returncode in (255, 124):
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"ssh: {alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
        return 255 if r.returncode == 255 else 1
    return None


def _wanted(args) -> "tuple[List[str], List[str]]":
    """CLIs and extensions to check: --cli, else the box's saved setup answers, else both."""
    answers: dict = {}
    try:
        answers = (box.box_entry(box.load_site(args.site), args.alias).get("setup") or {}).get("answers") or {}
    except box.SiteError:
        pass
    if args.cli:
        clis = _clis(args.cli)
    elif "agent_clis" in answers:
        clis = [c for c in answers["agent_clis"] if c in CLIS]
    else:
        clis = list(DEFAULT_CLIS)
    return clis, list(EXTENSIONS) if answers.get("agents.extensions", "yes") != "no" else []


def cmd_check(args) -> int:
    try:
        clis, exts = _wanted(args)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    r = _ssh(args.alias, probe_script(clis, bool(exts)), timeout=180)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    lines, problems, fixes = read_check(r.stdout, args.alias, clis, exts)
    if args.json:
        print(json.dumps({"alias": args.alias, "lines": lines, "problems": problems, "fixes": fixes}))
    else:
        print("\n".join(lines + ["verdict: " + ("problems: " + "; ".join(problems) if problems else "ok")]
                        + (["fix (the skill runs these; a login needs the user only to enter its code):"]
                           + [f"  {f}" for f in fixes] if fixes else [])))
    return 1 if problems else 0


def cmd_install(args) -> int:
    try:
        clis = _clis(args.cli)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    exts = list(EXTENSIONS) if args.extensions else []
    if not clis and not exts:
        print("error: nothing to install: name --cli and/or --extensions")
        return 1
    script = install_script(clis, exts)
    if not args.run:
        print(f"dry run: on {args.alias}, as your own user (no root):")
        for c in clis:
            print(f"- {c}: if `{c} --version` fails, `{CLIS[c]['install']}`, then `{c} --version`")
        if clis:
            print("- ~/.local/bin on PATH for ssh commands (first line of ~/.bashrc; ~/.zshenv for zsh) and login "
                  "shells (~/.bash_profile or ~/.profile), each file backed up once to <file>.ttp-bak")
        if exts:
            print(f"- {', '.join(exts)} on every Cursor and VS Code server build there, checked with --list-extensions")
        print("rerun with --run")
        return 0
    r = _ssh(args.alias, script, timeout=1800)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    lines, ok = read_install(r.stdout)
    print("\n".join(lines + [f"verdict: {'ok' if ok and r.returncode == 0 else 'failed'}; "
                             f"then `{BOX_PY} agents check {args.alias}`"]))
    return 0 if ok and r.returncode == 0 else 1


def cmd_login(args) -> int:
    cli = args.cli
    if args.status:
        r = _ssh(args.alias, status_script(cli), timeout=90)
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout) or "unknown"
        print(f"{cli} on {args.alias}: {st}")
        return 0 if st == "logged-in" else 1
    if args.code_stdin:
        r = _ssh(args.alias, code_script(cli), timeout=60, input=sys.stdin.readline())
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout)
        print(f"code passed to the pending {cli} login; then `{BOX_PY} agents login {args.alias} {cli} --status`"
              if st == "sent" else f"no pending {cli} login on {args.alias}: start one with "
              f"`{BOX_PY} agents login {args.alias} {cli}`")
        return 0 if st == "sent" else 1
    r = _ssh(args.alias, login_script(cli), timeout=120)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    st = _state(r.stdout)
    if st == "logged-in":
        print(f"{cli} on {args.alias}: already logged in")
        return 0
    if st == "no-cli":
        print(f"{cli} is not installed on {args.alias}: `{BOX_PY} agents install {args.alias} --cli {cli} --run` first")
        return 1
    shown = relay_lines(r.stdout)
    if st not in ("started", "pending") or not any("https://" in ln for ln in shown):
        print(f"{cli} login on {args.alias} did not show a sign-in link ({st or 'no answer'})"
              + ("".join(f"\n  {ln}" for ln in shown)))
        return 1
    print(f"{cli} login on {args.alias} is waiting for the user (tell them, word for word):")
    print("\n".join(f"  {ln}" for ln in shown))
    if cli == "claude":
        print(f"If the page then shows a code to paste back, pass it on with: "
              f"`{BOX_PY} agents login {args.alias} claude --code-stdin` (the code on stdin; it only completes "
              "this box's pending login and is never stored)")
    print(f"then: `{BOX_PY} agents login {args.alias} {cli} --status`  (exit 0 logged in)")
    return 0


# ---- editor-defaults: the user's own machine ----

def settings_paths(home: Optional[pathlib.Path] = None, system: Optional[str] = None) -> Dict[str, pathlib.Path]:
    """Each editor's user settings.json here, for editors whose user folder exists."""
    home = home or pathlib.Path.home()
    system = system or platform.system()
    if system == "Darwin":
        base = home / "Library" / "Application Support"
    elif system == "Windows":
        base = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    found = {}
    for name, folder in (("Cursor", "Cursor"), ("VS Code", "Code")):
        user = base / folder / "User"
        if user.is_dir():
            found[name] = user / "settings.json"
    return found


def _blank_jsonc(text: str) -> str:
    """`text` with comments and trailing commas turned into spaces: same offsets, plain JSON."""
    out = list(text)
    i, n, in_str = 0, len(text), False
    last_comma = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str, last_comma = True, None
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in text[i:j]]
            i = j
            continue
        elif ch == ",":
            last_comma = i
        elif ch in "]}":
            if last_comma is not None:
                out[last_comma] = " "
            last_comma = None
        elif not ch.isspace():
            last_comma = None
        i += 1
    return "".join(out)


def _top_key_value_span(blank: str, key: str) -> Optional["tuple[int, int]"]:
    """(start, end) of the top-level `key`'s value in blanked JSON, or None."""
    depth, i, n = 0, 0, len(blank)
    while i < n:
        ch = blank[i]
        if ch == '"':
            j = i + 1
            while j < n and blank[j] != '"':
                j += 2 if blank[j] == "\\" else 1
            if depth == 1 and json.loads(blank[i:j + 1]) == key and blank[j + 1:].lstrip().startswith(":"):
                k = blank.index(":", j + 1) + 1
                while blank[k].isspace():
                    k += 1
                _, end = json.JSONDecoder().raw_decode(blank, k)
                return k, end
            i = j + 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return None


def merge_defaults(text: str, exts: Sequence[str] = EXTENSIONS) -> "tuple[Optional[str], List[str]]":
    """(new text or None when nothing changes, the extensions added); ValueError when it cannot merge.

    Comments and everything else in the file stay as they are: only the setting's list is written.
    """
    blank = _blank_jsonc(text) if text.strip() else "{}"
    try:
        doc = json.loads(blank)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON with comments ({e.msg} at line {e.lineno})")
    if not isinstance(doc, dict):
        raise ValueError("the settings file is not a JSON object")
    have = doc.get(SETTING)
    if have is not None and not (isinstance(have, list) and all(isinstance(x, str) for x in have)):
        raise ValueError(f"{SETTING} is not a list of extension ids")
    have = have or []
    add = [e for e in exts if e.lower() not in {h.lower() for h in have}]
    if not add:
        return None, []
    new_list = json.dumps(have + add)
    if not text.strip():
        return "{\n    " + json.dumps(SETTING) + ": " + new_list + "\n}\n", add
    span = _top_key_value_span(blank, SETTING)
    if span:
        return text[:span[0]] + new_list + text[span[1]:], add
    brace = blank.index("{")
    comma = "," if doc else ""
    return text[:brace + 1] + "\n    " + json.dumps(SETTING) + ": " + new_list + comma + text[brace + 1:], add


def cmd_editor_defaults(args) -> int:
    paths = {f"file {p}": pathlib.Path(p).expanduser() for p in args.settings} if args.settings else settings_paths()
    if not paths:
        print("no Cursor or VS Code user settings on this machine: it is not where the editor runs; the box's "
              f"servers get the extensions from `{BOX_PY} agents install <box-alias> --cli none --extensions --run`, "
              "rerun after an editor update")
        return 0
    rc = 0
    for name, path in paths.items():
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            new, add = merge_defaults(text)
        except (OSError, ValueError) as e:
            print(f"{name}: left unchanged: {e} ({path})")
            rc = 1
            continue
        if new is None:
            print(f"{name}: {SETTING} already has {', '.join(EXTENSIONS)} ({path})")
            continue
        if not args.run:
            print(f"{name}: would add {', '.join(add)} to {SETTING} ({path}); rerun with --run")
            continue
        if path.exists():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.ttp-bak-{stamp}")
            backup.write_bytes(path.read_bytes())
        else:
            backup = None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.ttp-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        print(f"{name}: added {', '.join(add)} to {SETTING} ({path}"
              + (f"; backup {backup.name})" if backup else ")"))
    return rc


def register(sub) -> None:
    ag = sub.add_parser("agents", help="agent CLIs, logins and editor extensions on a box") \
        .add_subparsers(dest="agents_cmd", required=True)
    c = ag.add_parser("check", help="read-only: CLIs, PATH, logins and editor extensions, each gap with its fix")
    c.add_argument("alias")
    c.add_argument("--cli", help="comma-separated CLIs to check (default: the box's setup answer, else claude,codex)")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)
    i = ag.add_parser("install", help="CLIs, PATH lines and editor extensions (dry run unless --run)")
    i.add_argument("alias")
    i.add_argument("--cli", default=",".join(DEFAULT_CLIS), help="comma-separated, or none (default: claude,codex)")
    i.add_argument("--extensions", action="store_true", help="also install the extensions on every editor server")
    i.add_argument("--run", action="store_true")
    i.set_defaults(func=cmd_install)
    lg = ag.add_parser("login", help="start a CLI's headless login on the box and show its sign-in link and code")
    lg.add_argument("alias")
    lg.add_argument("cli", choices=tuple(CLIS))
    how = lg.add_mutually_exclusive_group()
    how.add_argument("--status", action="store_true", help="exit 0 logged in, 1 not (or pending)")
    how.add_argument("--code-stdin", action="store_true", help="pass the code from stdin to the pending login")
    lg.set_defaults(func=cmd_login)
    e = ag.add_parser("editor-defaults", help="add both extensions to remote.SSH.defaultExtensions on this machine "
                                              "(dry run unless --run; only on the user's yes)")
    e.add_argument("--settings", action="append", help="a settings.json path (default: Cursor's and VS Code's here)")
    e.add_argument("--run", action="store_true")
    e.set_defaults(func=cmd_editor_defaults)
