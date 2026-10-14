---
"tt-project": patch
---

Heal checks honour `ttp machines outage`: while the check's machine or `resource` (the alias, or the alias followed by `-` or `:`) has a known outage in the machine ledger, an unhealthy result gets no fix, task or alert, an unknown stretch sends no `unknown_cap_s` message, and the status reads `known outage until <time>` in the project's home zone. A check still failing once the outage ends starts a fresh episode with its grace. A check back from an unknown stretch (75 or 255) longer than its grace (at least 15 min) with no open self-fix task also starts fresh, keeping its fix history so `max_fixes` per `window_h` still holds. Known gap: a reboot does not start a fresh heal episode; the fix cap and any open self-fix task carry across reboots.
