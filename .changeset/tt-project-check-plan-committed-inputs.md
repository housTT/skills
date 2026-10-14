---
"tt-project": patch
---

`tt-project`: a harness check plan can bind a path to `"committed"` instead of a SHA-256 digest, so a frequently edited watcher or schedule JSON is covered as committed at HEAD without refreshing a digest or moving a pinned definition commit. A stale digest's error now says how to fix it.
