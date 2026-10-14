---
"tt-project": patch
---

Harness commits (charter, memory, schedules) no longer fail unseen: every failure is logged to `daemon.log` with git's exit code and stderr; lock files a killed commit left in the harness's `.git` (older than `harness_git.stale_lock_s`, 600 s, with no git running there) are moved aside to `.git/ttp-stale-locks/`, never deleted; the git timeout is 120 s and configurable (`harness_git.timeout_s`); and charter and memory files a failed commit left uncommitted are committed by the next successful commit or the daemon's sweep (`harness_git.sweep_every_s`).
