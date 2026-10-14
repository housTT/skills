---
"tt-project": patch
---

A task whose worktree sits on another branch than the task's recorded branch now recovers without the coordinator. When the worktree's branch has commits of this task's own (or of the task it continues), the task adopts that branch and keeps the worktree. When it has none, its untracked, ignored and modified files move to the project's `worktree-leftovers/t<id>-<date>/` (modified tracked files also go into a patch). The worktree is then made again on the task's branch, and no branch is deleted. Each recovery is logged as a `worktree_recovered` event and named in the worker's start note. A case neither rule covers (commits from an unrelated task, submodules set up, or the branch checked out elsewhere) blocks at once with its reason, instead of after several retries.
