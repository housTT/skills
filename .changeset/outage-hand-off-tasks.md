---
"tt-project": patch
---

Outage hand-offs become tracked tasks. When a shared machine stays down or held past its grace and its
recovery owner cannot act, or records no change-ledger entry within `heal.outage_owner_budget_min`
(default 60 min), the machine ledger queues one task per outage episode in the fallback project (else a
detecting project), due `heal.outage_task_due_min` (default 30) after it is queued. Past due and still
open, it pages that project once at high severity, then `heal.outage_escalate_min` (default 30) later
alerts the user once. Done and cancelled close an outage task; a failed one stays open and escalates.
Each project creates the task only in its own database (a daemon note carries it to another project).
Open outage tasks show in `ttp status`, the web app and the blocker sweep, with their age and due time.
The `heal.outage_*_min` settings are validated (1 minute to a week); `ttp config` refuses a bad value and
an invalid one in project.json keeps the default. A mute that covers a device hold now asks after 1 h by
default (`screen.hold_mute_escalate_h`, up to 6); the outage task never depends on mutes.

Accepted gaps: there are no dedicated tests for two daemons routing the same outage at once, or for
escalation across a daemon crash or a routing claim that lapses. The routing daemon's own budgets apply.
