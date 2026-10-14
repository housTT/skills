---
"tt-project": patch
---

Harness tasks that run at the same time no longer share one checkout: while another harness task's run works in the harness checkout, a new one gets a git worktree of the harness of its own (`state/harness-worktrees/t<id>`, branch `ttp/harness-t<id>`). When its run ends the daemon merges its commits into the harness, removes the worktree once it is clean, and restarts itself if the merge changed `runtime/`. A merge that conflicts is undone, kept for a retry, and reported to the coordinator once per branch head. The harness prompt asks workers to stage files by name, never `git add -A`.

The duplicate-issue check (watchdup) counts only issues of one condition class as same-subject duplicates: a key's subject and trailing number segments are dropped, keyed issues count each reopen, and the warning lists the keys.

`ttp setup`, `ttp upgrade` and `ttp release --apply` skip their whole-disk sync when `TTP_TEST_NO_SYNC` is set; the test suite sets it, since under other suites' disk load one sync from a test subprocess blocked for minutes and timed the test out.
