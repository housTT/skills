---
"tt-project": patch
---

Project lease (opt-in, `failover.lease_remote`): one live coordinator per project across hosts. The
daemon takes and renews a compare-and-swap lease on a separate git remote before coordinator turns and
dispatch, and a host that does not hold it starts no model work, pushes or chat replies. Clock-free
expiry, an epoch fencing token for runs (`TTP_LEASE_EPOCH`), one `lease:` line in `ttp status`, and
`ttp lease check|show`. Without the setting nothing changes.
