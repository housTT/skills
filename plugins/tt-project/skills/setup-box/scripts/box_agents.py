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
- Glean: `install` also adds the HTTP MCP server `glean` to each CLI (Claude Code user scope, Codex
  `[mcp_servers.glean]`), left alone when right, fixed with a backup when wrong. Its URL comes from
  `--glean-url`, the site file's `mcp.glean` (the box's entry, else top level) or this machine's own
  agent configs, never from a repo; with none it skips with one line. `login <alias> <cli> --mcp glean`
  runs the CLI's paste-the-redirect-URL OAuth sign-in on the box; `check` reports Glean per CLI as
  missing, signed out or working.
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
MCP = "glean"
MCP_URL_HINT = ('set its URL with --glean-url, or in the site file as "mcp": {"glean": "<url>"} (site.md), '
                "or run setup from a machine whose Claude Code, Codex or Cursor already has a `glean` server")


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


def _mcp_read(cli: str) -> str:
    """Shell: the `glean` entry of the CLI at $b into $g (empty when missing), its URL into $u.

    Only the URL, type and status lines are kept: a server's headers never leave the CLI."""
    if cli == "claude":
        return (f'g=$(cd "$HOME" && $T "$b" mcp get {MCP} </dev/null 2>/dev/null | grep -E "^ *(URL|Type|Status):"); '
                'u=$(printf "%s\\n" "$g" | sed -n "s/^ *URL: *//p" | head -n 1); ')
    return (f'g=$($T "$b" mcp get {MCP} --json </dev/null 2>/dev/null | grep -E \'^ *"(name|url|type)":\'); '
            'u=$(printf "%s\\n" "$g" | sed -n \'s/^ *"url": *"\\([^"]*\\)".*/\\1/p\' | head -n 1); ')


def _mcp_status(cli: str) -> str:
    """Shell: the sign-in state word of the CLI's `glean` server into $a (Claude Code's health check status,
    Codex's auth_status)."""
    if cli == "claude":
        return 'a=$(printf "%s\\n" "$g" | sed -n "s/^ *Status: *//p" | head -n 1); '
    return (f'a=$($T "$b" mcp list --json </dev/null 2>/dev/null | awk \'/"name": *"{MCP}"/{{f=1}} '
            'f && /"auth_status"/{print; exit}\' | sed \'s/.*: *"\\([^"]*\\)".*/\\1/\'); ')


def _mcp_working(cli: str, status: str) -> Optional[bool]:
    """True signed in and working, False signed out, None another state (an error to show)."""
    s = status.strip().lower()
    if cli == "claude":
        return True if "connected" in s else False if "needs auth" in s else None
    return True if s in ("o_auth", "oauth", "bearer_token") else False if s == "not_logged_in" else None


def probe_script(clis: Sequence[str], editors: bool = True, mcp: bool = True) -> str:
    """Read-only shell run on the box; prints key=value lines and never prints a token."""
    s = _TIMEOUT
    for c in clis:
        s += (f'echo "ni_{c}=$(command -v {c} 2>/dev/null)"; '
              f'echo "login_{c}=$("${{SHELL:-/bin/sh}}" -lc \'command -v {c}\' </dev/null 2>/dev/null | tail -n 1)"; '
              + _find(c) + f'echo "bin_{c}=$b"; '
              f'if [ -n "$b" ]; then echo "ver_{c}=$($T "$b" --version </dev/null 2>/dev/null | head -n 1)"; '
              f'if {_authed(c)}; then echo auth_{c}=yes; else echo auth_{c}=no; fi; '
              + ((_mcp_read(c) + f'if [ -z "$g" ]; then echo mcp_{c}=missing; else '
                  + _mcp_status(c) + f'echo "mcp_{c}=configured"; echo "mcpurl_{c}=$u"; echo "mcpauth_{c}=$a"; fi; ')
                 if mcp else "")
              + 'fi; ')
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


def read_check(out: str, alias: str, clis: Sequence[str], exts: Sequence[str],
               glean: Optional[str] = None) -> "tuple[List[str], List[str], List[str]]":
    """(report lines, problems, fixes) from the probe's key=value lines; `glean` is the configured URL."""
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
                     + (", logged in" if authed else ", not logged in") + _read_mcp(kv, c, alias, glean, problems, fix))
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


def _read_mcp(kv: Dict[str, str], c: str, alias: str, glean: Optional[str], problems: List[str], fix) -> str:
    """The Glean part of a CLI's check line: missing, on another URL, signed out, working, or the CLI's own
    status; each gap goes to problems with its fix. Missing with no URL configured is not a gap."""
    state = kv.get(f"mcp_{c}")
    if state is None:
        return ""
    install = f"{BOX_PY} agents install {alias} --cli {c} --run"
    if state == "missing":
        if not glean:
            return f", {MCP} not set up (no URL configured: {MCP_URL_HINT})"
        problems.append(f"{c}: {MCP} missing")
        fix(install)
        return f", {MCP} missing"
    url, auth = kv.get(f"mcpurl_{c}", ""), kv.get(f"mcpauth_{c}", "")
    if glean and url != glean:
        problems.append(f"{c}: {MCP} on another URL")
        fix(install)
        return f", {MCP} on another URL ({url or 'none'})"
    working = _mcp_working(c, auth)
    if working:
        return f", {MCP} working"
    problems.append(f"{c}: {MCP} " + ("signed out" if working is False else f"not working ({auth or 'no status'})"))
    fix(f"{BOX_PY} agents login {alias} {c} --mcp {MCP}  (relay its sign-in link to the user)")
    return f", {MCP} configured, " + ("signed out" if working is False else f"not working ({auth or 'no status'})")


def _mcp_add(cli: str) -> str:
    """Shell: add the `glean` server at $G to the CLI at $b (Claude Code in user scope)."""
    if cli == "claude":
        return f'(cd "$HOME" && $T "$b" mcp add --scope user --transport http {MCP} "$G" </dev/null >/dev/null 2>&1); '
    # Codex starts the OAuth sign-in right after adding and waits for its callback: the entry is written
    # first, the wait is cut short, and `login --mcp glean` signs in.
    return f'$TA "$b" mcp add {MCP} --url "$G" </dev/null >/dev/null 2>&1; '


def _mcp_remove(cli: str) -> str:
    """Shell: back up the CLI's config file once per run, then remove its `glean` server."""
    if cli == "claude":
        f, rm = '"$HOME/.claude.json"', f'(cd "$HOME" && $T "$b" mcp remove {MCP} -s user </dev/null >/dev/null 2>&1); '
    else:
        f, rm = '"${CODEX_HOME:-$HOME/.codex}/config.toml"', f'$T "$b" mcp remove {MCP} </dev/null >/dev/null 2>&1; '
    return (f'cf={f}; if [ -f "$cf" ]; then cp -p "$cf" "$cf.ttp-bak-$STAMP"; echo "mcpbak_{cli}=$cf.ttp-bak-$STAMP"; fi; '
            + rm)


def mcp_install_script(clis: Sequence[str], url: str) -> str:
    """Shell: the `glean` server at `url` for each CLI; left alone when right, else (re)added after a backup."""
    s = (f'G={shlex.quote(url)}; STAMP=$(date +%Y%m%d-%H%M%S); '
         'TA=; command -v timeout >/dev/null 2>&1 && TA="timeout 20"; ')
    for c in clis:
        s += (_find(c) + f'if [ -z "$b" ]; then echo "mcp_{c}=failed (no {c})"; fail=1; else '
              + _mcp_read(c) + f'if [ "$u" = "$G" ]; then echo "mcp_{c}=present"; else st=added; '
              'if [ -n "$g" ]; then st=fixed; ' + _mcp_remove(c) + 'fi; ' + _mcp_add(c) + _mcp_read(c)
              + f'if [ "$u" = "$G" ]; then echo "mcp_{c}=$st"; else echo "mcp_{c}=failed"; fail=1; fi; fi; fi; ')
    return s


def install_script(clis: Sequence[str], exts: Sequence[str], glean: Optional[str] = None) -> str:
    """Idempotent shell run on the box: CLIs, PATH lines, the Glean server, editor extensions; key=value lines."""
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
    if clis and glean:
        s += mcp_install_script(clis, glean)
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
        elif k.startswith("mcp_"):
            lines.append(f"{k[4:]}: {MCP} {v}")
            ok = ok and not v.startswith("failed")
        elif k.startswith("mcpbak_"):
            lines.append(f"  {k[7:]}: {MCP} was on another URL; config backed up to {v}")
        elif k == "ext" and v == "none":
            lines.append("editor servers: none on the box yet (an editor's first Remote-SSH connect installs one)")
        elif k == "ext":
            path, ext, state = (v.split("|") + ["", ""])[:3]
            lines.append(f"editor {server_label(path)}: {ext} {state}")
            ok = ok and state != "failed"
    return lines, ok


def _login_key(cli: str, mcp: Optional[str]) -> str:
    return f"{cli}-{mcp}" if mcp else cli


def _login_ready(cli: str, mcp: Optional[str]) -> str:
    """Shell: exit 0 with a state line when there is nothing to sign in to (no CLI or server, or signed in)."""
    s = _find(cli) + 'if [ -z "$b" ]; then echo state=no-cli; exit 0; fi; '
    if not mcp:
        return s + f'if {_authed(cli)}; then echo state=logged-in; exit 0; fi; '
    return (s + _mcp_read(cli) + 'if [ -z "$g" ]; then echo state=no-mcp; exit 0; fi; ' + _mcp_status(cli)
            + 'case "$a" in *Connected*|o_auth|oauth|bearer_token) echo state=logged-in; exit 0 ;; esac; ')


def login_script(cli: str, mcp: Optional[str] = None) -> str:
    """Start the CLI's headless login on the box, detached, unless it is logged in or one is pending.

    Its stdin is a fifo held open, so a code the sign-in page shows (or, for an MCP server, the redirect
    URL the browser ends on) can be passed back (`--code-stdin`); output goes to an owner-only log that is
    removed when the login ends. Claude Code reads a pasted redirect URL only from a terminal: its MCP
    sign-in runs under `script`, which gives it one.
    """
    c = _login_key(cli, mcp)
    if not mcp:
        cmd = f'$TL $b {CLIS[cli]["login"]}'
    elif cli == "claude":
        cmd = f'$TL env TERM=dumb script -qefc \\"$b mcp login {mcp} --no-browser\\" /dev/null'
    else:
        cmd = f'$TL $b mcp login {mcp} --no-browser'
    return (_TIMEOUT + f'umask 077; d={LOGIN_DIR}; mkdir -p "$d"; chmod 700 "$d"; ' + _login_ready(cli, mcp)
            + f'pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo state=pending; else '
            f'rm -f "$d/{c}.in" "$d/{c}.log"; mkfifo "$d/{c}.in" || {{ echo state=failed; exit 0; }}; '
            'TL=; command -v timeout >/dev/null 2>&1 && TL="timeout 900"; '
            'nohup setsid sh -c \'exec 3<>"$1"; eval "$2" <&3 >"$3" 2>&1; exec 3<&-; sleep 5; rm -f "$1" "$3" "$4"\' sh '
            f'"$d/{c}.in" "{cmd}" "$d/{c}.log" "$d/{c}.pid" </dev/null >/dev/null 2>&1 & '
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


def code_script(cli: str, mcp: Optional[str] = None) -> str:
    """Pass one line from stdin (the code the sign-in page showed, or the redirect URL) to the pending login."""
    c = _login_key(cli, mcp)
    end = "\\r" if mcp and cli == "claude" else "\\n"  # Enter on the terminal `script` gives it
    return (f'd={LOGIN_DIR}; IFS= read -r code || true; '
            f'pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null || [ ! -p "$d/' + c + '.in" ]; then '
            'echo state=no-login; exit 1; fi; '
            f'printf "%s{end}" "$code" >"$d/{c}.in"; echo state=sent')


def status_script(cli: str, mcp: Optional[str] = None) -> str:
    c = _login_key(cli, mcp)
    return ('st=$( (' + _TIMEOUT + _login_ready(cli, mcp) + 'echo state=logged-out) | sed -n "s/^state=//p" | head -n 1); '
            f'd={LOGIN_DIR}; pid=$(cat "$d/{c}.pid" 2>/dev/null); '
            'if [ "$st" = logged-out ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then st=pending; fi; '
            'echo "state=$st"')


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


def _site_entry(args) -> "tuple[dict, dict]":
    """(site file, the box's entry), each empty when missing."""
    try:
        site = box.load_site(args.site)
    except box.SiteError:
        return {}, {}
    try:
        return site, box.box_entry(site, args.alias)
    except box.SiteError:
        return site, {}


def local_mcp_url(home: Optional[pathlib.Path] = None, name: str = MCP) -> "tuple[Optional[str], str]":
    """(url, file) of the HTTP server `name` in this machine's own Claude Code, Codex or Cursor config."""
    home = home or pathlib.Path.home()
    for rel in (".claude.json", ".cursor/mcp.json"):
        try:
            srv = (json.loads((home / rel).read_text(encoding="utf-8")).get("mcpServers") or {}).get(name) or {}
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(srv, dict) and isinstance(srv.get("url"), str) and srv["url"].startswith("https://"):
            return srv["url"], f"~/{rel}"
    try:
        text = (home / ".codex/config.toml").read_text(encoding="utf-8")
    except OSError:
        return None, ""
    m = re.search(r'^\[mcp_servers\.' + re.escape(name) + r'\]\s*$(.*?)(?=^\[|\Z)', text, re.M | re.S)
    u = m and re.search(r'^\s*url\s*=\s*"(https://[^"]+)"', m.group(1), re.M)
    return (u.group(1), "~/.codex/config.toml") if u else (None, "")


def glean_url(args) -> "tuple[Optional[str], str]":
    """(url, where it came from): --glean-url, the site file's mcp.glean (box entry, then top level), then this
    machine's own agent configs. Never a default: the URL is the user's company's."""
    if getattr(args, "glean_url", None):
        return args.glean_url, "--glean-url"
    site, entry = _site_entry(args)
    for where, d in (("the box's site entry", entry), ("the site file", site)):
        u = (d.get("mcp") or {}).get(MCP) if isinstance(d.get("mcp"), dict) else None
        if isinstance(u, str) and u:
            return u, where
    return local_mcp_url()


def _wanted(args) -> "tuple[List[str], List[str]]":
    """CLIs and extensions to check: --cli, else the box's saved setup answers, else both."""
    answers = (_site_entry(args)[1].get("setup") or {}).get("answers") or {}
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
    r = _ssh(args.alias, probe_script(clis, bool(exts)), timeout=300)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    lines, problems, fixes = read_check(r.stdout, args.alias, clis, exts, glean_url(args)[0])
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
    url, where = glean_url(args) if clis else (None, "")
    script = install_script(clis, exts, url)
    skip = [f"{MCP}: skipped, no URL configured: {MCP_URL_HINT}"] if clis and not url else []
    if not args.run:
        print(f"dry run: on {args.alias}, as your own user (no root):")
        for c in clis:
            print(f"- {c}: if `{c} --version` fails, `{CLIS[c]['install']}`, then `{c} --version`")
        if clis:
            print("- ~/.local/bin on PATH for ssh commands (first line of ~/.bashrc; ~/.zshenv for zsh) and login "
                  "shells (~/.bash_profile or ~/.profile), each file backed up once to <file>.ttp-bak")
        if clis and url:
            print(f"- the {MCP} MCP server ({url}, from {where}) in each CLI: left alone when right, else re-added "
                  "after a backup of its config; then `agents login <box-alias> <cli> --mcp glean`")
        if exts:
            print(f"- {', '.join(exts)} on every Cursor and VS Code server build there, checked with --list-extensions")
        print("\n".join(skip + ["rerun with --run"]))
        return 0
    r = _ssh(args.alias, script, timeout=1800)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    lines, ok = read_install(r.stdout)
    print("\n".join(lines + skip + [f"verdict: {'ok' if ok and r.returncode == 0 else 'failed'}; "
                             f"then `{BOX_PY} agents check {args.alias}`"]))
    return 0 if ok and r.returncode == 0 else 1


_CALLBACK_PORT = re.compile(r"redirect_uri=https?%3A%2F%2F(localhost|127\.0\.0\.1)%3A(\d+)")


def callback_forward(lines: Sequence[str]) -> Optional[str]:
    """`<port>:<host>:<port>` for `ssh -L`, from the localhost callback an OAuth sign-in link shows, if any."""
    return next((f"{m.group(2)}:{m.group(1)}:{m.group(2)}" for m in map(_CALLBACK_PORT.search, lines) if m), None)


def cmd_login(args) -> int:
    cli, mcp = args.cli, args.mcp
    what = f"{cli} {mcp} sign-in" if mcp else f"{cli} login"
    again = f"{BOX_PY} agents login {args.alias} {cli}" + (f" --mcp {mcp}" if mcp else "")
    if args.status:
        r = _ssh(args.alias, status_script(cli, mcp), timeout=150)
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout) or "unknown"
        print(f"{cli}{f' {mcp}' if mcp else ''} on {args.alias}: {st}")
        return 0 if st == "logged-in" else 1
    if args.code_stdin:
        r = _ssh(args.alias, code_script(cli, mcp), timeout=60, input=sys.stdin.readline())
        bad = _ssh_failed(args.alias, r)
        if bad is not None:
            return bad
        st = _state(r.stdout)
        print(f"passed to the pending {what}; then `{again} --status`"
              if st == "sent" else f"no pending {what} on {args.alias}: start one with `{again}`")
        return 0 if st == "sent" else 1
    r = _ssh(args.alias, login_script(cli, mcp), timeout=240)
    bad = _ssh_failed(args.alias, r)
    if bad is not None:
        return bad
    st = _state(r.stdout)
    if st == "logged-in":
        print(f"{cli}{f' {mcp}' if mcp else ''} on {args.alias}: already signed in")
        return 0
    if st == "no-cli":
        print(f"{cli} is not installed on {args.alias}: `{BOX_PY} agents install {args.alias} --cli {cli} --run` first")
        return 1
    if st == "no-mcp":
        print(f"{cli} on {args.alias} has no {mcp} server: `{BOX_PY} agents install {args.alias} --cli {cli} --run` "
              f"first ({MCP_URL_HINT})")
        return 1
    shown = relay_lines(r.stdout)
    if st not in ("started", "pending") or not any("https://" in ln for ln in shown):
        print(f"{what} on {args.alias} did not show a sign-in link ({st or 'no answer'})"
              + ("".join(f"\n  {ln}" for ln in shown)))
        return 1
    print(f"{what} on {args.alias} is waiting for the user (tell them, word for word):")
    print("\n".join(f"  {ln}" for ln in shown))
    if mcp:
        fwd = callback_forward(shown)
        print("After signing in, the browser ends on a localhost page that may not load: the user copies that "
              f"page's full address and gives it to you; pass it on with `{again} --code-stdin` (on stdin; it only "
              "completes this pending sign-in and is never stored)."
              + (f" Or, before signing in, the user forwards the port from their own machine with "
                 f"`ssh -N -L {fwd} <their ssh alias for {args.alias}>` and the page completes it."
                 if fwd else ""))
    elif cli == "claude":
        print(f"If the page then shows a code to paste back, pass it on with: "
              f"`{again} --code-stdin` (the code on stdin; it only completes "
              "this box's pending login and is never stored)")
    print(f"then: `{again} --status`  (exit 0 signed in)")
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
    c.add_argument("--glean-url", help="the Glean MCP URL to expect (default as for install)")
    c.set_defaults(func=cmd_check)
    i = ag.add_parser("install", help="CLIs, PATH lines and editor extensions (dry run unless --run)")
    i.add_argument("alias")
    i.add_argument("--cli", default=",".join(DEFAULT_CLIS), help="comma-separated, or none (default: claude,codex)")
    i.add_argument("--extensions", action="store_true", help="also install the extensions on every editor server")
    i.add_argument("--glean-url", help="the Glean MCP URL (default: the site file's mcp.glean, else this machine's "
                                       "own agent configs)")
    i.add_argument("--run", action="store_true")
    i.set_defaults(func=cmd_install)
    lg = ag.add_parser("login", help="start a CLI's headless login on the box and show its sign-in link and code")
    lg.add_argument("alias")
    lg.add_argument("cli", choices=tuple(CLIS))
    lg.add_argument("--mcp", choices=(MCP,), help="sign the CLI in to this MCP server instead of the CLI's own login")
    how = lg.add_mutually_exclusive_group()
    how.add_argument("--status", action="store_true", help="exit 0 logged in, 1 not (or pending)")
    how.add_argument("--code-stdin", action="store_true", help="pass the code from stdin to the pending login")
    lg.set_defaults(func=cmd_login)
    e = ag.add_parser("editor-defaults", help="add both extensions to remote.SSH.defaultExtensions on this machine "
                                              "(dry run unless --run; only on the user's yes)")
    e.add_argument("--settings", action="append", help="a settings.json path (default: Cursor's and VS Code's here)")
    e.add_argument("--run", action="store_true")
    e.set_defaults(func=cmd_editor_defaults)
