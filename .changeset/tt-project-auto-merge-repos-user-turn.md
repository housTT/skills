---
"tt-project": patch
---

`tt-project`: the coordinator's config_set may add repos to `delivery.auto_merge_repos` only in the turn that carries the user's own message, and logs each change as a `config_grant` event naming that message; outside that turn, or to remove a repo, it is refused whatever it quotes (a charter Policies line granting auto-merge does not turn it on by itself).
