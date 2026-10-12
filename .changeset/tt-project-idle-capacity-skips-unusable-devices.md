---
"tt-project": patch
---

The idle-capacity wake skips devices the project cannot or does not use: paused ones (already), machines with an open change ledger entry that holds their device work (a held, paused or drained broker or queue, said as a phrase; file names, bare words and recovery switches such as auto reset or power cycle do not count), and devices no open task names and no task run in the last `coordinator.idle_capacity_use_days` (new key, 7; 0 turns the filter off) named. When the coordinator handles an `idle_capacity` event without a task naming a device, the decline is kept per device in the database, so its next idle stretch (after a pause, trouble or a restart) waits both its grace and the doubling rearm from its last event, never less than an undeclined device; an open task naming the device clears it.
