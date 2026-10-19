---
"tt-project": patch
---

With `failover.lease_remote` set, only the project lease holder takes standby state snapshots (`failover.standby`). A host that does not hold the lease neither snapshots nor raises a stale-snapshot alert, and closes one it raised while it held the lease; after taking the lease back it snapshots at once and counts staleness from the takeover. With no lease configured nothing changes.
