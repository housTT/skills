# Charter grants

The daemon (runtime `grants.py`) compares the charter's Brief and Policies with the live delivery settings, model-free, at setup and the first tick after a charter or delivery edit: the Policies `Auto-merge repositories:` line against `delivery.auto_merge_repos`, and a grant of pushes to the push branch against `delivery.push_allowed` and (for main, master or the remote's default) `delivery.allow_protected_push_branch`.
A push grant the config lacks is applied only on the user's word on record (the Brief, or a Policies entry `CHARTER.history.md` logs with a user message), with one low alert and open asks about the setting closed.
Auto-merge is report-only: the coordinator can write that charter line itself, so the daemon never writes `delivery.auto_merge_repos` (the coordinator sets it with `config_set` in the turn carrying the user's word).
Everything else (any auto-merge mismatch, no user word, the charter also forbidding it, a setting set back after it was applied) is one keyed `grant_mismatch` event for the coordinator, never an ask; nothing the charter does not grant is ever set.
