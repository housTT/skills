---
"tt-project": patch
---

A provider's account change that keeps the same billing kind (for example one usage-billed account to another) no longer wakes the coordinator: with nothing released and no task tied to the old limits it raises no event and no feed line. Budget-change events that remain are batched at normal severity, so one never starts a coordinator turn on its own.
