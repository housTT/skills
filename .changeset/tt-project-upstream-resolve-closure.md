---
"tt-project": patch
---

`ttp upstream --resolve` in a sandbox that cannot write the user's files queues the resolution in the run's directory, as `--ack` does, and the daemon records it when the run ends. A code task's hand-off can list the upstream notes it fixes in `resolves`; once the push queue lands the change, each is recorded as fixed in the version that batch pushed.
