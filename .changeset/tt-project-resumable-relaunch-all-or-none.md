---
"tt-project": patch
---

`ttp detach --resumable <name> -- <cmd>` marks a local detached job as safe to rerun from the start. When an abrupt host reboot cuts such a job short while its task still waits on it, the daemon starts the same command again on the next boot, with no model run, and keeps the task asleep on its `retry_when`. A job is relaunched at most `budget.max_job_relaunches` times (default 3); past that the task wakes with a note saying why.

Each boot on which a relaunch follows an abrupt, unplanned reboot counts toward `budget.max_reboot_losses`, as a wake would, so a job that keeps taking the host down still gets its task blocked as a reboot loop. Orderly reboots and planned host cycles relaunch without counting.

Relaunching a task's resumable jobs is all or none: when one of them cannot start, the jobs already relaunched are stopped again (each gets a `.lost` note saying why) and the task wakes with no job of it left running. This holds across a daemon restart partway through: jobs an earlier daemon life relaunched on the same boot count as started, are stopped too, and their relaunch is recorded once, so the boot still counts. The relaunches are recorded only once every job started, so a boot that wakes the task counts once toward `max_reboot_losses`, not also as a relaunch boot. A resumable job queued for the daemon but not started before a long reboot is relaunched and no longer marked lost by its stale queue entry.
