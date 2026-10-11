---
"tt-project": patch
---

Add `runner.pin_verify` (`lenient` by default, or `strict`) and a persisted task
`pin_verify` override. Strict verification refuses model/effort-pinned runs when
the provider cannot report its resolved session, blocking without a model turn,
spend, an attempt or automatic retries. Record every verification outcome with
the run, retain overrides in derived pinned tasks, and document the policy.
