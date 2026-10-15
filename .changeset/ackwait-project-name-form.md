---
"tt-project": patch
---

An ack-wait refusal now names a project only as `from <name>`, `<name> project` or `<name>'s`, and never
refuses a wait while a detached job is in flight, so a project with a generic name no longer blocks
a legitimate wait such as "CI to respond".
