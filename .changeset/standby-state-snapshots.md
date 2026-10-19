---
"tt-project": patch
---

Standby state snapshots: with `failover.standby` set (a machine alias or an absolute directory),
the daemon ships a consistent snapshot of the database (secrets scrubbed), the harness and open
tasks' branches every `failover.snapshot_every_s`, keeping `failover.snapshot_keep`. Run folders
are left out. `ttp replicate` shows its age, `--now` takes one, `--verify` checks it against its
manifest and `--restore DIR --into PATH` rebuilds a project from it. `ttp status` gains a one-line
snapshot age and a stale snapshot raises one keyed observation. Nothing changes without a standby.
