---
"tt-project": patch
---

Standby state snapshots: with `failover.standby` set (a machine alias or an absolute directory),
the daemon ships a consistent snapshot of the database (secrets scrubbed), the harness and open
tasks' branches every `failover.snapshot_every_s`, keeping the newest `failover.snapshot_keep` and the full
one they build on. Run folders are left out. `ttp replicate` shows its age, `--now` takes one, `--verify` checks it against its
manifest and `--restore DIR --into PATH` rebuilds a project from it. `ttp status` gains a one-line
snapshot age, and a standby not known to hold the current state for too long raises one keyed
observation. The README documents it. Nothing changes without a standby.
