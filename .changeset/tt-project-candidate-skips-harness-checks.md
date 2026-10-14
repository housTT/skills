---
"tt-project": patch
---

The candidate `ttp checks` (every code task's checks) and `ttp push --own` no longer run `delivery.push_checks` entries tagged `"purpose": "harness"`; they report them skipped, never passed. In a worktree of another repo such a check failed with a raw git error (exit 20) and failed every check run there. `ttp checks --purpose harness`, `ttp push` and the push queue still run them. `ttp check-plan` started outside the repo that has its definition commit, or outside any git repo, now exits 66 with a message saying so (and naming the project's own repo when it has the commit) instead of git's raw error.
