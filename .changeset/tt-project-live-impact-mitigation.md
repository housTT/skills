---
"tt-project": patch
---

A hand-off that states `live_impact` (top level, or on a follow-up) now gets a priority-1 interim-mitigation
task with a due time (`coordinator.mitigation_due_h`, default 4) beside its fix. The blocker sweep and
`ttp status` show it with its due time; overdue, it wakes the coordinator at high effort; once the fix is
done, its temporary measures and ledger entries are listed for undo.
