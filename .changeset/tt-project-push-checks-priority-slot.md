---
"tt-project": patch
---

`tt-project`: push-queue batches and `ttp push` no longer wait behind task checks for a host-wide
checks slot. Each push branch gets one priority slot beyond the cap, so its checks start at once;
task checks keep their cap and order. The batch log and `ttp status` show when a push holds it.
