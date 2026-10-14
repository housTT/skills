---
"tt-project": patch
---

`tt-project`: the watchdog kills a confirmed-hung systemd daemon (after its stack dump) before `systemctl --user restart`, so the restart no longer waits out the unit's stop timeout and reports a plain restart instead of the stop/kill/start escalation
