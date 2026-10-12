---
"tt-project": patch
---

Add `ttp alerts <project> --ack <message-id> --why "<reason>"` to close a keyed alert episode with a durable reason, retaining history and preserving the original acknowledgement on retries. Unknown ids and missing or blank reasons are refused.

Retire an existing `heal:<name>` user alert when escalation moves to the coordinator or a known fault covers its current confirmed failure, including faults identified after a self-fix task escalated. Readers hide it immediately and the daemon records why it closed without claiming recovery. Prolonged outages and unknown checks retain their escalation rules.
