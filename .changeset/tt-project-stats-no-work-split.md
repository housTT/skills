---
"tt-project": patch
---

`ttp stats` leaves runs with no model call (failed sign-in, or no prompt tokens) out of per-run averages and reports them apart, adds the median per run, model calls per run and first-call context, and takes `--since`, `--until` and `--split-at` for before/after comparisons.
