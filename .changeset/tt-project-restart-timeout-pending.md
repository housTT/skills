---
"tt-project": patch
---

`tt-project`: A daemon restart whose `systemctl --user restart` times out is now a pending restart,
not a failure. `ttp restart` and `ttp upgrade` wait the usual re-check window for the new daemon and
exit 0 once it completes a tick on the runtime on disk. They still fail with exit 1 and a high alert
when no daemon comes up in the window (rolled back as before) or the new one reports another version.
