# Listening for replies, per host

The listener prints each message for this chat and exits (with `--once`) or streams.
Replies to this chat plus broadcast alerts arrive. Others' replies never do.
Each line starts with the message id: `[#<id> coordinator] <text>`.

## Acknowledge what you showed

- Always pass `--ack <id>`: the highest message id you have shown the user, `0` before the first.
- Only acknowledged messages count as read. Anything printed to a chat that closed or a
  connection that dropped comes back on the next listen.
- A message can therefore arrive twice. Skip ids you have already shown.

## Remote projects and dropped connections

- For a project on another machine the listener runs there over ssh. A dropped connection is
  retried (5 s, doubling to 2 min) until it is back; each message prints once per listener.
- With `--once`, a batch that arrived before a drop ends the listener normally (exit 0).
- Exit codes: `0` messages shown or `--timeout` reached; `2` the project refused it (unknown chat
  or project); `75` still unreachable when `--timeout` ran out (nothing is lost: start it again).

## Claude Code

- Run in the background: `ttp listen <name> --chat <id> --once --ack <last id shown>`.
- Its exit wakes you. Print the messages, then start it again with the new last id.
- Always keep exactly one listener running per attached project.
- NEVER run it in the foreground. NEVER add `sleep` loops.

## Cursor

- Run `ttp listen <name> --chat <id> --ack <last id shown>` as a monitored background shell.
- Notify on output. Each line is a message; relay it.
- When the shell ends or you restart it, pass the last id you relayed.

## Codex

- No background wake-up. At the start of each user turn run:
  `ttp listen <name> --chat <id> --once --timeout 1 --ack <last id shown>`
- Also run it right after `ttp say`, with `--timeout 90`.
- The user can always ask "any news from <name>?".

## Any host

- Lost the chat id → `ttp connect <name>` again (new id, same project).
- Lost the last id → `--ack 0` re-shows what this chat has not acknowledged.
- Listener errors → `ttp doctor <name>`.
