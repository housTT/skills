---
"tt-project": patch
---

coordinator: a user turn's messages are kept for the next turn, once, after any task_add rejection a resend can fix (an unknown kind, a bad dependency, start time or spec), not only a null title or spec. Duplicates, already tried or reviewed work, caps and a dropped resource name still handle the message. task_add now rejects a kind other than question, code, review, plan, harness, work or one a project's own `kind-<name>.md` prompt adds.
