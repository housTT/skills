---
"tt-project": patch
---

Push queue and host reboots: a batch that ended without an outcome while the host rebooted is
reported as "cut short by the HH:MM reboot", counts no try and no backoff, and runs again as soon
as the daemon is back. Batches cut by reboots alone raise no high alert; a new high alert,
`push_not_landing`, fires once when approvals have waited `delivery.push_landing_alert_h`
(default 4, 0 = off) with nothing landed despite retries, with the attempt count and how many a
reboot cut short, and clears when a batch lands.
