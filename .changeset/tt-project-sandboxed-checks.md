---
"tt-project": patch
---

Queue sandboxed detached checks through the daemon so tool process namespaces cannot kill them. The daemon records the host process identity and cancels earlier checks before starting replacements. Queued checks survive restarts, retain reboot recovery, and settle successful checks through `on_pass` without a model wake.
