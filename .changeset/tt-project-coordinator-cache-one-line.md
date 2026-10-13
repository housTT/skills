---
"tt-project": patch
---

`tt-project`: the coordinator cache status is worded once, on the server. The web app's status payload carries each window's `line` (the same `coordinator_cache_line()` text as `ttp status` and the daily review), and the web app shows it as is instead of building its own copy. Coordinator turns from before runs recorded cache telemetry count as legacy, not missing (the daemon backfills them at start from their output where the stored tokens match), and the "request count unknown" clause appears only when some turn records a request count, so projects on providers that report none no longer show it. Known gap: coordinator runs that end through the spawn-failure or reap-failure paths still record no cache telemetry, so they are counted as legacy.
