---
"tt-project": patch
---

Model-free `on_pass_cmd` callbacks can report progress and send notes to other projects with the
waiting task's attribution. Note-only context preserves deferred delivery in the original run
without enabling other commands that require a worker run, even with inherited run variables.
A callback still counts as a run for every user-only guard (`ttp say`, user-only project and account
settings, obligations, opening PRs). Notes it queued are filed whatever it ends with (failure, timeout,
cancellation, a daemon restart), once and as the waiting task's.
