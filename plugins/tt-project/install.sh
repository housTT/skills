#!/bin/sh
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Installs tt-project for each of Claude Code, Codex and Cursor found on this machine, then runs
# `ttp setup`. Safe to rerun: it updates the same clone and config entries in place. A step that
# fails does not stop the other tools or `ttp setup`: the failures are listed at the end, and the
# installer then exits 1.
#
# Usage, from a shell (set TT_PROJECT_REF to a release tag; empty installs the default branch):
#
# (TT_PROJECT_REF=; d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && git init -q "$d" && git -C "$d" fetch -q --depth 1 https://github.com/tenstorrent/skills.git "${TT_PROJECT_REF:-HEAD}" && git -C "$d" -c advice.detachedHead=false checkout -q FETCH_HEAD && if [ -f "$d/plugins/tt-project/install.sh" ]; then sh "$d/plugins/tt-project/install.sh"; else echo "tt-project: no installer at ${TT_PROJECT_REF:-the default branch}; set TT_PROJECT_REF to a release tag" >&2; exit 1; fi)
#
# The line fetches the one ref (a tag, branch or commit) and checks out FETCH_HEAD, rather than
# `git clone --branch`, which warns that an annotated tag "is not a commit".
#
# or `sh <checkout>/plugins/tt-project/install.sh` to install the commit that checkout has.
# The commit is copied into one clone kept at $XDG_DATA_HOME/tt-project/skills (default
# ~/.local/share/tt-project/skills): Claude Code and Codex use it as a local marketplace, and Cursor
# gets a copy of its plugin folder.
set -eu

MARKET=tenstorrent-skills
PLUGIN=tt-project

say() { printf 'tt-project: %s\n' "$*"; }
die() { printf 'tt-project: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"
command -v python3 >/dev/null 2>&1 || die "Python 3.9+ (python3) is required"

src=$(cd "$(dirname "$0")/../.." && pwd -P)
[ -f "$src/plugins/$PLUGIN/bin/ttp" ] || die "$src has no plugins/$PLUGIN; run install.sh from a checkout of the skills repository"
git -C "$src" rev-parse --verify -q HEAD >/dev/null || die "$src is not a git checkout"

data=${XDG_DATA_HOME:-$HOME/.local/share}/tt-project
dest=$data/skills
mkdir -p "$data"
dest_real=$( [ -d "$dest" ] && cd "$dest" && pwd -P || echo "" )
if [ "$dest_real" != "$src" ]; then
    if [ ! -e "$dest" ]; then
        git init -q "$dest"
    elif ! git -C "$dest" rev-parse --git-dir >/dev/null 2>&1 || [ "$(cd "$dest" && git rev-parse --show-toplevel)" != "$(cd "$dest" && pwd -P)" ]; then
        die "$dest exists and is not a git clone; move it away and rerun"
    fi
    git -C "$dest" fetch -q --depth 1 "$src" HEAD
    git -C "$dest" checkout -q -f --detach FETCH_HEAD
    # a one-line install's temporary checkout has no origin: fall back to the public repository
    url=$(git -C "$src" remote get-url origin 2>/dev/null || true)
    url=${url:-https://github.com/tenstorrent/skills.git}
    git -C "$dest" remote set-url origin "$url" 2>/dev/null || git -C "$dest" remote add origin "$url"
fi
plugin=$dest/plugins/$PLUGIN
[ -f "$plugin/bin/ttp" ] || die "$dest has no plugins/$PLUGIN after the update"
# The one-line install fetches the ref into FETCH_HEAD without a local tag: name the tag it fetched.
fetched_tag() {
    [ "$(git -C "$src" rev-parse -q --verify 'FETCH_HEAD^{commit}' 2>/dev/null)" = "$(git -C "$src" rev-parse HEAD)" ] || return 1
    t=$(sed -n "1s/^[0-9a-f]*[[:space:]]*tag '\(.*\)' of .*/\1/p" "$(git -C "$src" rev-parse --absolute-git-dir)/FETCH_HEAD")
    [ -n "$t" ] && printf '%s\n' "$t"
}
rev=$(git -C "$src" describe --tags --exact-match HEAD 2>/dev/null || fetched_tag || git -C "$src" rev-parse --short HEAD)
ver=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$plugin/.claude-plugin/plugin.json" | head -1)
say "installing $PLUGIN ${ver:-?} ($rev) from $dest"

# One install step of $tool. A failure is listed at the end and skips only the rest of that tool's steps.
failed=""
fail() {
    printf 'tt-project: %s: %s\n' "$tool" "$*" >&2
    failed="$failed
tt-project:   $tool: $*"
}
step() {
    "$@" && return 0
    rc=$?
    fail "\`$*\` failed (exit $rc)"
    return 1
}

claude_steps() {
    step claude plugin marketplace add "$dest" &&
        step claude plugin install "$PLUGIN@$MARKET" &&
        step claude plugin update "$PLUGIN@$MARKET"    # an earlier install from another source moves to this one
}

codex_steps() {
    # Codex refuses a CODEX_HOME that does not exist yet (installed, never run).
    step mkdir -p "${CODEX_HOME:-$HOME/.codex}" || return 1
    if ! out=$(codex plugin marketplace add "$dest" 2>&1); then
        case $out in
        *"different source"*)
            say "Codex had $MARKET from another source: replacing it with $dest"
            step codex plugin marketplace remove "$MARKET" && step codex plugin marketplace add "$dest" || return 1 ;;
        *) printf '%s\n' "$out" >&2; fail "\`codex plugin marketplace add $dest\` failed"; return 1 ;;
        esac
    else
        printf '%s\n' "$out"
    fi
    step codex plugin add "$PLUGIN@$MARKET"    # copies the current files into Codex's plugin cache
}

# Cursor loads a symlink in its local folder only when it points inside that folder, so it gets a copy.
cursor_steps() {
    local_dir=$HOME/.cursor/plugins/local
    step mkdir -p "$local_dir" &&
        step rm -rf "$local_dir/.$PLUGIN.new" &&
        step cp -R "$plugin" "$local_dir/.$PLUGIN.new" &&
        step rm -rf "$local_dir/$PLUGIN" &&
        step mv "$local_dir/.$PLUGIN.new" "$local_dir/$PLUGIN" || return 1
    say "Cursor: copied the plugin to $local_dir/$PLUGIN"
}

found=""
done_hosts=""
if command -v claude >/dev/null 2>&1; then
    found=1 tool="Claude Code"
    claude_steps && done_hosts="$done_hosts claude"
else
    say "Claude Code not found (no claude on PATH): skipped"
fi

if command -v codex >/dev/null 2>&1; then
    found=1 tool=Codex
    codex_steps && done_hosts="$done_hosts codex"
else
    say "Codex not found (no codex on PATH): skipped"
fi

if command -v cursor-agent >/dev/null 2>&1 || command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
    found=1 tool=Cursor
    cursor_steps && done_hosts="$done_hosts cursor"
else
    say "Cursor not found (no cursor or cursor-agent on PATH, no ~/.cursor): skipped"
fi

tool="ttp setup"
ttp_ok=""
step "$plugin/bin/ttp" setup && ttp_ok=1

[ -n "$found" ] || say "no Claude Code, Codex or Cursor found: install one, then rerun this installer"
if [ -n "$failed" ]; then say "finished with failed steps (listed below). To load it:"; else say "done. To load it:"; fi
for h in $done_hosts; do
    case $h in
    claude) say "  Claude Code: start a new session, or in an open one run /reload-plugins (and /reload-skills for the plain /tt-project)" ;;
    codex) say "  Codex: start a new session (open sessions do not see it)" ;;
    cursor) say "  Cursor: restart it, or run Developer: Reload Window" ;;
    esac
done
if [ -n "$ttp_ok" ]; then
    case ":$PATH:" in
    *":$HOME/.local/bin:"*) say "  ttp: ready in this shell" ;;
    *) say "  ttp: add ~/.local/bin to PATH in your shell profile, then open a new shell" ;;
    esac
fi
if [ -n "$failed" ]; then
    printf 'tt-project: these steps failed; fix them, then rerun this installer:%s\n' "$failed" >&2
    exit 1
fi
