# tt-project

Long-running, self-driving projects for Claude Code, Codex and Cursor. Give a project a brief and its
coordinator plans the work, hands it to headless workers in isolated git worktrees, watches pull
requests and logs, and stays inside a budget. It keeps going when you close the chat; any later chat
connects to it by name.

## Install

Needs Python 3.9+, `git`, and Claude Code, Codex or Cursor. In a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/tenstorrent/skills/HEAD/plugins/tt-project/install.sh | sh
```

Rerun it to update, then start a new agent session. Release tags (needed until tt-project is on the
default branch), manual steps and uninstall:
[docs/install.md](docs/install.md).

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
