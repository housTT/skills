# setup-box: set up a new box

A fresh box gets set up, not only checked. First ask the user what they want: not every box
needs a broker, a workspace or agent CLIs. Nothing is assumed: a suggested answer is offered, and
an unanswered question stays open.

The box needs its site entry first (`site.md`). Every command below is
`<plugin-root>/skills/setup-box/scripts/box.py`, shortened to `box.py`.

## 1. Ask

```
box.py setup questions <box-alias>                    # JSON batches for a question tool
box.py setup questions <box-alias> --format numbered  # a plain numbered prompt
```

- Claude Code: ask each batch with its multiple-choice question tool (`AskUserQuestion`): each
  batch has at most 4 questions of 2-4 options. Use each question's `header`, `question`,
  `multiSelect` and option `label`/`description` as given; map the picked labels back to their
  `value`. The tool's free-text choice is how the user types a value (a branch, a project name, a
  number of GB). A "Type it" option means: use the free-text choice.
- Agents without such a tool (Codex, Cursor): show the numbered prompt as it is and read the
  reply (`1 b`, `3 a,c`, or a typed value).
- Ask again only for follow-ups: run `questions` after each save; a follow-up (the broker's
  release or branch, the workspace's branch and ccache, the recovery fallback) appears once its
  question is answered.

| Question | Options |
|---|---|
| Device broker | install tt-device-mcp (which release or branch), or none: device work then takes `ttp lock <box-alias>-device -- <command>` |
| Host checks (read-only) | any of driver, firmware, hugepages, PCIe link |
| Workspace | a repo checkout and build under `workdir_root` (branch, ccache), or none |
| Python env | pip ttnn, the workspace's own env, or none |
| Agent CLIs | Claude Code, Codex, Cursor agent, or none (only if projects will run on the box) |
| Projects | which of the user's projects may use the box |
| Recovery | the project that recovers it, and a fallback |
| Device lock | shared by all the user's projects, or per project |
| Disk guard | free GB to keep under `workdir_root` (0 = off) |

## 2. Save

```
box.py setup save <box-alias> --set broker=none --set host_checks=driver,pcie ...
box.py setup save <box-alias> --json '{"broker": "install", "broker.ref": "latest-release"}'
```

- Answers go into the box's site entry (`setup`), with a fingerprint of each question.
- A rerun of `questions` asks only what is new, what changed wording or options since it was
  answered, and the follow-ups of an answer that changed. `--all` asks everything again.
- Lists are comma-separated; `none` alone clears a multi-choice answer.

## 3. Act, then check each answer

```
box.py setup plan <box-alias>
```

It refuses while a question is open. Otherwise it prints one step per answer: an action and the
check that proves it. Do each action, run its check, and report both. A failed check is reported,
not skipped.

- Broker, install: the tenstorrent/tt-device-mcp repository's documented installer and upgrade
  path at the chosen release or branch; never hand-edit an installed broker, and never start a
  second one. Check: `box.py bare check <box-alias>` shows the broker present and active.
- Broker, none: no broker is installed. Each command that touches the device runs as
  `ttp lock <box-alias>-device -- <command>`, held only for that command (`bare-metal.md`).
- Host checks: `box.py setup hostcheck <box-alias> [--only driver,pcie]` reads sysfs, /proc and
  `/dev/tenstorrent` listings only; it opens no device. Exit 0 ok, 1 a problem, 255 ssh failed.
  Fixes (driver, firmware, hugepages): tt-buddy's `recipes/developer-setup.md`.
- Workspace and Python env: tt-buddy's `recipes/workspace.md`, `recipes/tt-metal/build.md` and
  `recipes/tt-metal/env.md`, plus the notes in `bare-metal.md` (submodules, ccache, build in
  place). A broker job needs an env too: the `tt-buddy:run` skill's `skills/run/mcp.md`.
- Agent CLIs: `agents.md`. The user types any login.
- Projects: the box goes into each named project's charter Resources; the plan does not edit
  charters.
- Recovery, device lock and disk guard: `ttp machines add <box-alias>` with `--owner`/`--fallback`,
  `--shared <box-alias>-device` or `--unshared`, and `--min-free-gb`.

If tt-buddy is not installed, name it in the hand-off as the plugin that holds these recipes.

## Hand-off

As in `SKILL.md`, plus each plan step with its check's result.
