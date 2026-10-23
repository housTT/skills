---
"tt-model-bringup": patch
---

Let goal 12 explore arms in parallel. An explorer owns one hypothesis — predicts,
sizes its own sample, dispatches, reports — while a coordinator holds the current
best, the slot ledger and the budget, decides what is kept, and re-runs kept changes
together. Explorers do not accept their own arms, and runs are registered at launch
so the ledger stays usable when several explorers dispatch at once.

Adds sizing guidance: estimate a sample's CI time before dispatching, and size it to
what it has to separate rather than to a fixed task count. Shape follows the claim —
a concurrency change needs enough simultaneous conversations to load the pool and
scheduler, a precision or long-context change needs long free-running replies, a
cache change needs repeated multi-turn traffic.
