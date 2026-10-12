---
"tt-project": patch
---

Detached checks use their own temporary storage instead of the launching worker's scratch directory.
An inherited lifetime lock protects live checks and their commands; completion, lost-run probes,
and daemon recovery remove abandoned scratch after its owners exit. Foreground checks are unchanged.
