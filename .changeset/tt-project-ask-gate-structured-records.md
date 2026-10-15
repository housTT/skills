---
"tt-project": patch
---

The coordinator refuses a review or merge ask once when every PR it names already holds an unspent `pr_approve` record at the PR's current head, naming the record; `ask_user`'s new `not_covered` field (at least 30 characters saying what differs) sends it anyway. New config key `coordinator.approval_lookback_days` (default 30) bounds how old a record may be. The gate never reads the user's free-text words, and restriction, irreversible and other asks always go out; a re-ask after a free-text or relayed approval is left to the prompt's relay and ask rules.
