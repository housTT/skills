---
"tt-project": patch
---

An empty `coordinator.cache_ttl` (left by an older template) is read as `auto`, so the coordinator keeps its automatic 1h prompt cache instead of silently dropping to 5 min. `ttp doctor` reports the empty value and `ttp upgrade` sets it to `auto`; an explicit value is kept.
