---
"tt-project": patch
---

`tt-project`: the desktop notifier alarms once when a project's daemon has not beaten for 3 h (`daemon_down_after_s` in `notifier.json`) while its service should keep it running and its host answers, and once when it runs again; `ttp alerts --json --health` adds the daemon's health, with the heartbeat's age taken on its own host, and an older remote without the flag still delivers its alerts
