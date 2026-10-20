# Obligations

`ttp obligation` (runtime `obligations.py`): a deliverable owed to the user, tracked on its owner tasks until a receipt.
Each owner has a 2 h durable-progress clock.
A milestone (`key`, absolute `due`, `evidence_required`, set by the coordinator's `obligation` action) raises one high `obligation_overdue` episode at its due time without its receipt (`receipt <id> "<evidence>" --milestone <key>`), whatever the heartbeat or other progress.
Known gap: nothing checks that an owner can advance it, so an obligation whose owners all wait or stall is caught only by the 2 h progress rule (or a milestone falling due); a daemon-side coverage check was dropped for its false alarms on ordinary waits and review stacks.
