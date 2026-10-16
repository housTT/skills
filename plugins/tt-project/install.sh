#!/bin/sh
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Installs tt-project for each of Claude Code, Codex and Cursor found on this machine, then runs
# `ttp setup`. Safe to rerun: it updates the same clone and config entries in place.
#
# Usage, from a shell (set TT_PROJECT_REF to a release tag; empty installs the default branch):
#
# (TT_PROJECT_REF=; d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && git -c advice.detachedHead=false clone -q --depth 1 ${TT_PROJECT_REF:+--branch=$TT_PROJECT_REF} https://github.com/tenstorrent/skills.git "$d" && if [ -f "$d/plugins/tt-project/install.sh" ]; then sh "$d/plugins/tt-project/install.sh"; else echo "tt-project: no installer at ${TT_PROJECT_REF:-the default branch}; set TT_PROJECT_REF to a release tag" >&2; exit 1; fi)
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
    url=$(git -C "$src" remote get-url origin 2>/dev/null || true)
    if [ -n "$url" ]; then
        git -C "$dest" remote set-url origin "$url" 2>/dev/null || git -C "$dest" remote add origin "$url"
    fi
fi
plugin=$dest/plugins/$PLUGIN
[ -f "$plugin/bin/ttp" ] || die "$dest has no plugins/$PLUGIN after the update"
rev=$(git -C "$src" describe --tags --exact-match HEAD 2>/dev/null || git -C "$src" rev-parse --short HEAD)
ver=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$plugin/.claude-plugin/plugin.json" | head -1)
say "installing $PLUGIN ${ver:-?} ($rev) from $dest"

done_hosts=""
if command -v claude >/dev/null 2>&1; then
    claude plugin marketplace add "$dest"
    claude plugin install "$PLUGIN@$MARKET"
    claude plugin update "$PLUGIN@$MARKET"    # an earlier install from another source moves to this one
    done_hosts="$done_hosts claude"
else
    say "Claude Code not found (no claude on PATH): skipped"
fi

if command -v codex >/dev/null 2>&1; then
    if ! out=$(codex plugin marketplace add "$dest" 2>&1); then
        case $out in
        *"different source"*)
            say "Codex had $MARKET from another source: replacing it with $dest"
            codex plugin marketplace remove "$MARKET"
            codex plugin marketplace add "$dest" ;;
        *) printf '%s\n' "$out" >&2; die "codex plugin marketplace add failed" ;;
        esac
    else
        printf '%s\n' "$out"
    fi
    codex plugin add "$PLUGIN@$MARKET"    # copies the current files into Codex's plugin cache
    done_hosts="$done_hosts codex"
else
    say "Codex not found (no codex on PATH): skipped"
fi

# Cursor loads a symlink in its local folder only when it points inside that folder, so it gets a copy.
if command -v cursor-agent >/dev/null 2>&1 || command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
    local_dir=$HOME/.cursor/plugins/local
    mkdir -p "$local_dir"
    rm -rf "$local_dir/.$PLUGIN.new"
    cp -R "$plugin" "$local_dir/.$PLUGIN.new"
    rm -rf "$local_dir/$PLUGIN"
    mv "$local_dir/.$PLUGIN.new" "$local_dir/$PLUGIN"
    say "Cursor: copied the plugin to $local_dir/$PLUGIN"
    done_hosts="$done_hosts cursor"
else
    say "Cursor not found (no cursor or cursor-agent on PATH, no ~/.cursor): skipped"
fi

"$plugin/bin/ttp" setup

[ -n "$done_hosts" ] || say "no Claude Code, Codex or Cursor found: install one, then rerun this installer"
say "done. To load it:"
for h in $done_hosts; do
    case $h in
    claude) say "  Claude Code: start a new session, or in an open one run /reload-plugins (and /reload-skills for the plain /tt-project)" ;;
    codex) say "  Codex: start a new session (open sessions do not see it)" ;;
    cursor) say "  Cursor: restart it, or run Developer: Reload Window" ;;
    esac
done
case ":$PATH:" in
*":$HOME/.local/bin:"*) say "  ttp: ready in this shell" ;;
*) say "  ttp: add ~/.local/bin to PATH in your shell profile, then open a new shell" ;;
esac
