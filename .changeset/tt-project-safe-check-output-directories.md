---
"tt-project": patch
---

Keep detached-check scratch in project state, reject symlinked directories and lifetime locks,
and protect cleanup with directory descriptors. Detached and queued pushes now export a unique,
writable TTP_RUN_DIR recorded with the invocation and rebuilt after daemon restarts. Fail closed
before checks when output storage is unavailable; keep reports after scratch cleanup.
