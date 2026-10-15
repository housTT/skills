---
"tt-project": patch
---

`tt-project`: A review whose spec has exactly one `pr_branch:` line can publish the reviewed code task's head
onto that branch with `ttp push --own` (fast-forward only), even when the code task carried no `pr_branch` label.
