---
"tt-project": patch
---

Retain successful detached push commits as task landing evidence even when the worker hands off waiting or another non-done result. Reconcile markers on worker completion and daemon ticks so restarts recover missing records idempotently, and landed probes continue to work after marker and worktree cleanup.
