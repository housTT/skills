---
"tt-project": patch
---

The coordinator's ask gate refuses two more kinds of avoidable ask. A `human` ask must say who else
asked, or name them in `requester` (an inbound message id, an upstream note fingerprint, an issue or
PR URL the project has seen, or a role such as a reviewer or maintainer). A `restriction`,
`irreversible` or `human` ask about a rerun of a test, run, job, soak, benchmark or sweep, or about
moving a deadline, due date or milestone, is the coordinator's own call unless `not_own_call` says
why only the user can decide it; asks about money, credentials, user-only steps, other people's
requests or settings that need the user are exempt. Refusals and overrides are counted for the
daily review.

Known gaps, accepted for now: `restriction` asks are still checked only by the length of their
`least_disruptive` text (an ask about a restriction recorded with `memory_add` or one in the
charter is sent either way), and `requester` accepts any inbound message id, including one of the
user's own messages.
