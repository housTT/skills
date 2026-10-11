---
"tt-project": patch
---

Model-free `on_pass_cmd` callbacks can report progress and send notes to other projects with the
waiting task's attribution. Note-only context preserves deferred delivery in the original run
without enabling other commands that require a worker run, even with inherited run variables.
