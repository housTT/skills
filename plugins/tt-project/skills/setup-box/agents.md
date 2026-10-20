# setup-box: agents on the box

An agent that edits and builds on the box runs there, or drives the box over ssh from the project
box. Either way the work happens in the box's own checkout (`ssh-workflow.md`).

## Agent CLIs

| Agent | Install and check | Headless login |
|---|---|---|
| Claude Code | install it per its current official instructions; check with `claude --version` | `claude` then `/login`; the login link opens on any machine |
| Codex | install it per its current official instructions; check with `codex --version` | `codex login --device-auth` (a code typed on another machine; no browser on the box) |
| Cursor | Remote-SSH from the user's editor to `<box-alias>`; the `cursor-agent` CLI on the box if used, checked with `cursor-agent --version` | the editor's login carries over Remote-SSH |

- Install for the user only (no root), unless the user says otherwise, in the user's own account
  (`account.md`), never a shared admin login. Logins (agent CLIs, gh) live in the user's home.
- gh: a distro's packaged gh can be too old for a newer `hosts.yml`; install it from GitHub's own
  apt repository.
- Logins and tokens are typed by the user; never paste them into a prompt, task or file.

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
