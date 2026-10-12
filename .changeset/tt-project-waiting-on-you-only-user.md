---
"tt-project": patch
---

`tt-project`: "Waiting on you" in `ttp status`, the web app and `ttp overview` now counts only blocked
tasks that truly wait on the user: an open ask, an explicit user mark or a resource the user paused.
Tasks held by the coordinator, a resource, a time or an already answered ask get a neutral line instead
(`held: N blocked task(s) (waiting on the coordinator 2, a resource 1)`). All three use one classifier,
and an unknown reason defaults to the coordinator.
