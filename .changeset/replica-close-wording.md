---
"tt-project": patch
---

The replica-stale issue now closes with "the standby holds the current state": an unchanged check (nothing new, standby still current) also closes it, and does not contact the standby.
