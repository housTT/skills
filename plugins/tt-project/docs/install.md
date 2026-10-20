# Installing tt-project

The [README](../README.md) has the one-line install. This page covers what it does, the manual
steps per host, updates and uninstall.

## What the one-line install does

It fetches one commit of `tenstorrent/skills` with `git` and your own git credentials and runs
`plugins/tt-project/install.sh` from it. `TT_PROJECT_REF`, set at the front of the line, names a
release tag (a branch or commit also works); empty means the default branch. A ref without the
installer fails with a message to set `TT_PROJECT_REF` to a release tag.

The installer:

1. checks for `git` and Python 3.9+, and stops with a message naming what is missing;
2. keeps one clone at `~/.local/share/tt-project/skills` (under `$XDG_DATA_HOME` when set);
3. adds it as the `tenstorrent-skills` marketplace for Claude Code and Codex, and copies the plugin
   into Cursor's local plugin folder, for each tool it finds (it skips the others with a note);
4. runs `ttp setup`, which installs `ttp` in `~/.local/bin`, then checks that `ttp --version` runs.

If one tool's step fails, it still installs for the others and runs `ttp setup`, then lists the
failed steps and exits non-zero.

Rerun the line to update, or with another tag to switch: it updates that clone and the same entries
in place. Rerunning the line with the same `TT_PROJECT_REF` installs whatever that ref points at now:
a branch gives its latest commit, and a release tag the publisher moves forward as a release channel
gives the latest release. A tag that is never moved keeps you on exactly that release.

## Loading it

- Claude Code: start a new session, or in an open one run `/reload-plugins` (and `/reload-skills`
  for the plain `/tt-project`).
- Codex: start a new session; open sessions do not see it.
- Cursor: restart it, or run **Developer: Reload Window**. On Teams and Enterprise plans an admin
  must allow local plugin imports.
- `ttp`: works in an open terminal when `~/.local/bin` is already on its `PATH`; otherwise add that
  folder to `PATH` in your shell profile and open a new shell.

## Skills

To call a skill directly instead of just asking for a project:

| Host | Main skill | Harness skill | Device box skill |
|---|---|---|---|
| Claude Code | `/tt-project` | `/tt-project:tt-project-harness` | `/tt-project:setup-box` |
| Codex | `$tt-project:tt-project` | `$tt-project:tt-project-harness` | `$tt-project:setup-box` |
| Cursor | `/tt-project` | `/tt-project-harness` | `/setup-box` |

The device box skill gets a box (bare metal, a reservation or a Slurm job) and has agents edit,
build and run in their own checkout on it over ssh. Site values stay in a local
`~/.tt-project/boxes.json`, never in a repository.

Claude Code shows a plugin's skills under the plugin's name, so its own entry for the main skill is
`/tt-project:tt-project`. `ttp setup` adds a small personal skill,
`~/.claude/skills/tt-project/SKILL.md` (under `$CLAUDE_CONFIG_DIR` when set), listed as
`/tt-project`, that loads the plugin's skill. Only you can run it, so Claude still sees one
tt-project skill. If you already have your own skill named `tt-project`, setup leaves it alone.
`ttp setup --no-shortcut` removes the shortcut.

## Manual install

The same steps by hand, per host. You need Python 3.9+, `git` with `user.name` and `user.email`
set, and a logged-in agent CLI to run the project's workers. Set `TT_PROJECT_REF` to a release tag
or branch (the one-line install reads the same variable, set at its front).

Claude Code (a branch or tag goes after `#`):

```bash
export TT_PROJECT_REF=<release tag or branch>
claude plugin marketplace add "https://github.com/tenstorrent/skills.git#$TT_PROJECT_REF"
claude plugin install tt-project@tenstorrent-skills
claude plugin details tt-project    # Skills (3): setup-box, tt-project, tt-project-harness
"$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Codex (`--ref` takes a branch or tag):

```bash
codex plugin marketplace add tenstorrent/skills --ref $TT_PROJECT_REF
codex plugin add tt-project@tenstorrent-skills
"$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Cursor installs plugins from a team marketplace or a local folder, and neither takes a git ref, so
check out the ref with `git` and copy the plugin into Cursor's local plugin folder (Cursor skips a
symlink there that points outside that folder):

```bash
git init -q tt-skills-release && git -C tt-skills-release fetch -q --depth 1 https://github.com/tenstorrent/skills.git "$TT_PROJECT_REF" && git -C tt-skills-release -c advice.detachedHead=false checkout -q FETCH_HEAD
mkdir -p ~/.cursor/plugins/local
cp -R tt-skills-release/plugins/tt-project ~/.cursor/plugins/local/tt-project
~/.cursor/plugins/local/tt-project/bin/ttp setup
```

Then restart Cursor (or run **Developer: Reload Window**).

## Update

Rerun the one-line install. After a manual install, remove the marketplace and repeat the steps
above with the new ref, `ttp setup` included:

```bash
claude plugin marketplace remove tenstorrent-skills    # also uninstalls its plugins
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
```

When you follow a branch, Claude Code can also update in place (then run `ttp setup` as above):

```bash
claude plugin marketplace update tenstorrent-skills && claude plugin update tt-project@tenstorrent-skills
```

Running projects take in the new release within an hour and keep their work.

## Uninstall

Stop every project (`ttp list` names them), then remove the shortcut, the plugin and the per-user
files (project registry, saved keys, ttp's cache and the installed `ttp`):

```bash
ttp stop <name> --kill          # each project: ends its daemon and workers, removes its service
ttp setup --no-shortcut
claude plugin marketplace remove tenstorrent-skills
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
rm -rf ~/.cursor/plugins/local/tt-project
rm -rf ~/.tt-project ~/.local/bin/ttp "${XDG_CACHE_HOME:-$HOME/.cache}/ttp"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/tt-project"    # the one-line install's clone
```

Each project keeps its files in `<project root>/tt-project/`; delete that folder too to drop its
history. If you ran `ttp notifier install`, also remove its `tt-project-notifier` user service.

## Known issues

- Cursor install is verified by clone and `ttp setup` only, not end to end.
- The setup-box IRD route is tested against a fake `ird`; a live run needs a network that reaches the IRD service.
- The setup-box new-box flow (`new-box.md`) is tested with fakes only; its install steps have not yet run on a live box.
- Codex and Cursor spend is estimated from a price table (see [Limits](guide.md#limits)).
- Not on Codex: worker plugins. Not yet on Cursor: worker plugins, worker isolation, mid-run coordinator updates.
- Older Cursor builds print nothing until a run ends, so set `stall_s` above their longest run.
- A laptop pauses while it sleeps; use an always-on machine for round-the-clock work.
