---
"tt-project": patch
---

Host checks queue: checks of priority-1 tasks (live-impact work such as heal self-fixes and interim
mitigations) go ahead of other projects' waiters, taking turns by project, for at most
`checks.priority_turns` turns in a row (default 3, 0 off) while other checks wait.
