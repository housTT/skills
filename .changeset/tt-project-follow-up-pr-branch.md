---
"tt-project": patch
---

A follow-up (`continues` on a done task) carries another branch only through its own `pr_branch:` line: naming a finished task's branch (for example as the source of a cherry-pick) no longer labels it `pr_branch:`, so its `ttp push` to the push branch is not refused. A task that continues a failed or cancelled one still carries the branch that one carried, unless its spec names only the push branch or the coordinator sets `pr_branch` to `""` (task_add, or task_update before it starts); it is then labelled `no_pr_branch` and lands on the push branch.
