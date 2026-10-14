---
"tt-project": patch
---

Relaunching a task's resumable detached jobs after a reboot is all or none: when one of them cannot start, the jobs already relaunched are stopped again (each gets a `.lost` note saying why) and the task wakes with no job of it left running. The relaunches are recorded only once every job started, so a boot that wakes the task counts once toward `max_reboot_losses`, not also as a relaunch boot.
