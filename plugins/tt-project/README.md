# tt-project

Long-running, self-driving projects for Claude Code, Codex and Cursor. Give a project a brief and its
coordinator plans the work, hands it to headless workers in isolated git worktrees, watches pull
requests and logs, and stays inside a budget. It runs on your own machine or an always-on box, keeps
going when you close the chat, and any later chat can connect to it by name.

## Install

You need Python 3.9+, `git`, and Claude Code, Codex or Cursor. Paste this line into a terminal:

```bash
(TT_PROJECT_REF=; command -v git >/dev/null || { echo "tt-project: install git first" >&2; exit 1; }; d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && git init -q "$d" && git -C "$d" fetch -q --depth 1 https://github.com/tenstorrent/skills.git "${TT_PROJECT_REF:-HEAD}" && git -C "$d" -c advice.detachedHead=false checkout -q FETCH_HEAD && if [ -f "$d/plugins/tt-project/install.sh" ]; then sh "$d/plugins/tt-project/install.sh"; else echo "tt-project: no installer at ${TT_PROJECT_REF:-the default branch}; set TT_PROJECT_REF to a release tag" >&2; exit 1; fi)
```

Set `TT_PROJECT_REF` at its front to a release tag of `tenstorrent/skills`; empty installs the
default branch. It installs for each of Claude Code, Codex and Cursor it finds and ends with a
checked `ttp` in `~/.local/bin`. Rerun it to update. Then start a new agent session.
Details, manual steps, update and uninstall: [docs/install.md](docs/install.md).

## Quick start

1. Create a project. In a chat, say "start a tt-project called `demo` in this repository: keep the
   README accurate", or from a shell inside a git repository:

   ```bash
   ttp new demo --describe "Keep the README accurate."
   ```

2. Open the web app. `ttp new` ends with a link, `http://127.0.0.1:<port>/#token=<token>`;
   `ttp web demo` prints it again.
3. Talk to it. In any chat, say "connect to project demo", then write to it; its replies and alerts
   come back to that chat. From a shell: `ttp say demo "<your words>"` and `ttp status demo`.

In Claude Code, `/tt-project` calls the skill directly; other hosts and skills:
[docs/install.md](docs/install.md#skills).

## Learn more

- [Guide](docs/guide.md): how it works, budgets, model tiers, parallel work, notifications,
  security and limits.
- [Install](docs/install.md): what the installer does, manual steps, update, uninstall, known issues.
- [Providers](docs/providers.md): what each agent CLI supports.
- [Design notes](docs/design.md): for people changing tt-project itself.
