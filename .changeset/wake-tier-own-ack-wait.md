---
"tt-project": patch
---

A wait whose retry_when is the project's own `ack_wait.py` wrapper or a command in `ack_wait.probe_commands`
now wakes at light tier, like `ttp ack-wait probe`.
