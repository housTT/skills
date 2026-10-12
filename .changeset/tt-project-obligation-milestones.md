---
"tt-project": patch
---

`tt-project`: obligations can carry milestones: a stable key, an absolute due time (epoch, or ISO with a
time zone offset) and the evidence required, set on the coordinator's `obligation` action. An owner
records a checked milestone with `ttp obligation receipt <id> "<evidence>" --milestone <key>`, which
leaves the obligation open. A milestone still without its receipt at its due time raises one high
`obligation_overdue` episode, even with fresh owner progress or a healthy heartbeat; its receipt, the
obligation's receipt or a drop retires that event if the coordinator has not handled it yet. `ttp status`
and the web app show each open obligation with its next milestone in the project's home time zone. The
2 h durable-progress rule is unchanged. Known gap: nothing checks whether an owner can advance an
obligation, so one whose owners all wait or stall is caught only by the 2 h rule (or a milestone falling due).
