---
"tt-project": patch
---

`tt-project`: a code task with no branch on record that published its change as a PR (its pr_url, or the `pr` its hand-off names, such as work in another repository's clone) now gets its review queued by the daemon at the PR's head, its diff read from the hosting service (`gh pr view`), review only. Without a PR, the blocker sweep's diagnosis says no branch is on record, so the daemon cannot read the task's diff
