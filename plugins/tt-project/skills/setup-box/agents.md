# setup-box: agents on the box

An agent that edits and builds on the box runs there, or drives the box over ssh from the project
box. Either way the work happens in the box's own checkout (`ssh-workflow.md`).

## Agent CLIs, logins and editor extensions

The skill runs every step below itself, in the user's own account (`account.md`), never a shared
admin login, and never as root. `box.py` is `<plugin-root>/skills/setup-box/scripts/box.py`.

```
box.py agents check <box-alias>                      # read-only: each gap and the command that fixes it
box.py agents install <box-alias> --run              # Claude Code and Codex CLIs, PATH lines
box.py agents install <box-alias> --cli none --extensions --run   # editor extensions only
box.py agents login <box-alias> claude|codex         # a headless login; shows its link and code
box.py agents login <box-alias> claude|codex --status
box.py agents editor-defaults --run                  # on the machine the editor runs on, only on the user's yes
```

| Agent | Install | Login (on the box; the user enters a code in their browser) | Check |
|---|---|---|---|
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` | `claude auth login` | `claude --version`, `claude auth status` |
| Codex | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` | `codex login --device-auth` | `codex --version`, `codex login status` |
| Cursor agent | per its official instructions | its own login | `cursor-agent --version` |

- `install` is idempotent: a CLI whose `--version` works is left alone. It puts `~/.local/bin` on
  PATH for a plain `ssh <box-alias> <command>` (the first line of `~/.bashrc`, before any early
  return for non-interactive shells; `~/.zshenv` for zsh) and for login shells (`~/.bash_profile`
  or `~/.profile`), each file backed up once to `<file>.ttp-bak`.
- Editors over Remote-SSH (Cursor, VS Code): the editor's own login carries over only to its own
  AI. The Claude Code (`anthropic.claude-code`) and Codex (`openai.chatgpt`) extensions run on the
  box's editor server and log in with the box's `~/.claude` and `~/.codex`, the same logins as the
  CLIs. `install --extensions` installs both on every server build there (`~/.cursor-server`,
  `~/.vscode-server`) and checks them with `--list-extensions`. A server build appears on the
  editor's first connect: connect once, then install.
- An editor update brings a new server build without the extensions. `editor-defaults` adds both
  to `remote.SSH.defaultExtensions` in Cursor's and VS Code's user settings, so every new build on
  every box gets them. It edits the user's own machine: only on their yes (the setup question),
  merged into the list with existing entries and comments kept, the file backed up first. When
  the editor runs on another machine than the agent, it says so and changes nothing; the box check
  then finds a new build without them and `install --extensions` fixes it.
- `login` starts the CLI's headless login on the box, detached, and prints its sign-in link and
  code. Tell the user both word for word; they open the link and enter the code. When Claude
  Code's page then shows a code to paste back, the user gives it to the agent, which passes it on
  with `login <box-alias> claude --code-stdin` (on stdin; single-use, it only completes that
  pending login). Then `login ... --status`. The login and its tokens stay on the box: never copy
  `~/.claude` or `~/.codex` between machines, never print or log a token, and never put an API
  key in a prompt, task or file.
- `check` reports each missing CLI, PATH gap, login and editor extension with the command that
  fixes it; run that command yourself. A login is the one step that needs the user: its code.
- gh: a distro's packaged gh can be too old for a newer `hosts.yml`; install it from GitHub's own
  apt repository. Its login is the user's (`gh auth login`, a code in their browser).

## Building and running: tt-buddy

Link to the tt-buddy plugin's recipes and skills; do not copy them here.

| Need | tt-buddy file |
|---|---|
| A new box: toolchain, drivers, firmware checks | `recipes/developer-setup.md` |
| Where checkouts and build trees live | `recipes/workspace.md` |
| Build tt-metal | `recipes/tt-metal/build.md` |
| Environment variables for runs | `recipes/tt-metal/env.md` |
| Run tests | `recipes/tt-metal/test.md` |
| Device runs (jobs, queueing, logs) | the `tt-buddy:run` skill |
| Device runs through tt-device-mcp | the `tt-buddy:run` skill's `skills/run/mcp.md` |

- tt-buddy is a separate plugin, installed only by the user's choice. When it is missing, name it in
  the hand-off as the plugin that holds these recipes; do not reconstruct them.
- Device runs on a box with a device broker go through it (tt-device-mcp), never by opening the
  device directly.

## MCP endpoints

- A site's MCP endpoints (device broker, other services) come from the site file
  (`mcp` in `site.md`), never from a repo or this skill.
