---
"tt-project": patch
---

A heal self-fix task's spec now says that under a recorded known outage or `known_fault`, the check ending at exit 75 (known outage, unknown) is the expected end state: the worker hands off once the check reports it instead of trying to force a pass.
