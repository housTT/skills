---
"tt-project": patch
---

Add `ttp check-plan` to run an independently committed harness coverage definition without requiring its future HEAD in a candidate publication whitelist. Exact file digests and mandatory commands stay bound to the reviewed definition; candidate-purpose definitions still reject unknown full commit identities. Git errors, missing inputs, identity mismatches and selected-command failures remain distinct.
