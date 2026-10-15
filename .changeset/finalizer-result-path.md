---
"tt-project": patch
---

Document that task-owned `on_pass` finalizers resumed under a new run derive output paths from `TTP_RESULT` (falling back to `TTP_RUN_DIR`), stay inside the project's run directory, and pin the original evidence by path and hash.
