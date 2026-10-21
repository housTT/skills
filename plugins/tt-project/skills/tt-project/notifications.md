# Notifications

Default channels need no accounts or setup.

| Channel | Setup | Reaches the user |
|---|---|---|
| Attached chats | none | alerts appear in every attached chat |
| Web app | one click: "Turn on notifications" | browser notification while the page is open |
| Desktop notifier | `ttp notifier install` (run it on the user's workstation, say so) | native notification on this machine, all projects |

- The notifier polls local projects directly and remote ones over the user's ssh.
- A remote host ssh cannot reach for 20 min (`host_down_after_s` in `notifier.json`) gets one
  notification, since its projects cannot page, and one more when it answers again. The 20 min
  count only polls in a row: after a sleep or a stopped notifier it starts again.
- A project whose daemon has not beaten for 3 h (`daemon_down_after_s`) while its service should keep it
  running gets one notification naming `ttp restart`, and one more when it runs again.
- Desktop notifications play a sound. Backends: terminal-notifier, else osascript (macOS; allow
  Script Editor in System Settings > Notifications); notify-send in a desktop session (Linux).
- `ttp notifier test` sends one through the same path and says whether it worked, with the fix
  if not; exit 1 = failed. macOS does not report display: in a terminal it asks if you saw it.
- `ttp notifier install`, `ttp setup` and `ttp upgrade` send that test once per installed ttp version.
  `ttp setup` and `ttp upgrade` also restart the notifier on the new code; `ttp status` says when it
  runs older code (`ttp notifier restart`).
- Each failed delivery goes to `~/.tt-project/notifier.log`; a failing stretch raises one chat
  alert from the projects on that machine, cleared once a delivery works again.
- Only `high` severity notifies by default: decisions, reviews, merges, funds, outages.
- The user can ask the coordinator for more or fewer alerts.

## Slack (optional)

- Reading Slack links (bug reports, threads): workers use the user's Slack connector or
  enterprise search if installed. The user just pastes links.
- Posting team-facing updates to a channel: workers can use the Slack connector when the
  user asks. It posts as the user, so it does NOT notify the user.
- A team that already has a Slack bot token can enable DMs from it:
  `ttp secret slack --email <user@company>` (token typed in a terminal), then
  `ttp config <name> notify.slack true`. Replies in the bot's DM route back to the project.
