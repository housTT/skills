---
"tt-project": patch
---

`tt-project`: fewer coordinator turns that decide nothing. A plain `task_done` (no follow-up
proposed, no live task depending on it, no waiting parent) waits `coordinator.batch_s` for company
even while a worker slot is idle; normal upstream notes and info observations batch too. A mute
that ended with nothing muted, or by its `end_when` probe, is recorded without a turn. `wait_stale`
skips a probe that is only a date comparison until its epoch passes. The blocker sweep carries a
valid, not-yet-due `still_blocked` forward as one CARRIED line that needs no action unless the item
changed, skips an interval sweep where every item is carried, and its header names the accepted
`start_after` format and both required fields. `coordinator.blocker_sweep_h`,
`blocker_sweep_min_gap_h` and `review_backlog_h` are now accepted by `ttp config`.
