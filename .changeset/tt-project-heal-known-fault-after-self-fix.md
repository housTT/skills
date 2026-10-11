---
"tt-project": patch
---

Keep a non-outage heal finding quiet when a self-fix adds `known_fault`, clearing the
check's task and escalation state before it can alert. Real outages still escalate.
