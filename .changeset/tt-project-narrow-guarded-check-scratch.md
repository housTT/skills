---
"tt-project": patch
---

Limit managed temporary storage to check processes holding its lifetime guard. Push and batch
supervisors, Git hooks and deployment commands keep their ordinary temporary directory.
Leave legacy allocations and their reports untouched, without migration or reclamation.
