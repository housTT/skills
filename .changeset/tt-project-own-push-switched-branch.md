---
"tt-project": patch
---

`ttp push --own` refuses when the worktree has another of the task's own `ttp/t<id>-...` branches checked out than the one its run was given, naming both branches. Before, it published that branch's head onto the task's carried pr_branch, which may belong to another task.
