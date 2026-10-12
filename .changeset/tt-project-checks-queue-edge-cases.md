---
"tt-project": patch
---

Settle queued checks that already started before applying queue expiry, preserving their running
state and exit codes after daemon downtime. Keep a newly published queue marker when a probe or
withdrawal previously read no marker.
