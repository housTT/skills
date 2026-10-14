---
"tt-project": patch
---

`ttp check-plan --definition-at-head` (purpose harness only) reads the definition from HEAD's own
commit, so a change to a covered input and its refreshed digest land in one commit instead of a
second commit to move the pinned definition; an uncovered input's error prints its digest at HEAD.
`ttp checks` no longer refuses to run because of unstaged edits to tracked files the repository
ignores, such as a log a running watcher appends to; a staged edit or any other change still blocks.
