---
"tt-project": patch
---

The daemon writes every stack dump (SIGUSR1, SIGUSR2 and the tick overrun or stall dump) to one file, `state/daemon-stacks.log`, which `ttp.watchdog` copies to `state/incidents/`; past 1 MiB it moves to `.1` at daemon start. `logs/tick-stacks.log` is no longer written.
