---
"tt-project": patch
---

`tt-project`: the daemon wakes the coordinator, model-free, when a device resource the project holds sits idle with a free worker slot, a green or yellow gate and nothing queued for it (`idle_capacity` event; `coordinator.idle_capacity_s`, default 1500 s, 0 turns it off). One event per idle stretch; one still idle is raised again only after `coordinator.idle_capacity_rearm_s` (6 h), doubling up to 48 h. Paused resources, resources in trouble and machines with an open ledger condition never count.
