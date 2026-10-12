---
"tt-project": patch
---

Observation mutes can end on a probe: `observation_mute` takes an optional `end_when` (a read-only shell probe, exit 0 once the mute can end, 1 while not), and `hours` becomes optional with it. The daemon runs the probe model-free every 10 minutes; its pass ends the mute with the usual summary event, and a broken or timed-out probe is reported once while the mute keeps its time. No mute lasts more than 7 days from its start however often it is extended; its end summary then says it reached the cap.
