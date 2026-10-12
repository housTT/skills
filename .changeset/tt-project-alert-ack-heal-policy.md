---
"tt-project": patch
---

Add `ttp alerts <project> --ack <message-id> --why "<reason>"` to acknowledge a keyed alert episode with a durable reason. An episode whose condition is over closes (releasing a machine-wide claim); one that still holds moves to the feed and its repeats stay quiet until it clears, so a new occurrence alerts again. A live outage (logout, unreachable hosts, full disk, machine or whole-box heal outage) is refused. History and the original acknowledgement are kept on retries; unknown ids and missing or blank reasons are refused.

Retire an existing `heal:<name>` user alert when escalation moves to the coordinator or a known fault covers its current confirmed failure, including faults identified after a self-fix task escalated. Readers hide it immediately and the daemon records why it closed without claiming recovery. Prolonged outages and unknown checks retain their escalation rules.
