---
"tt-project": patch
---

Queue sandboxed detached checks through the daemon so tool process namespaces cannot kill them. The daemon records the host process identity and cancels earlier checks before starting replacements. Queued checks survive restarts, retain reboot recovery, and settle successful checks through `on_pass` without a model wake.

Each queued request carries an id and is published, settled and checked under the queue's lock: a newer request that arrives while the daemon starts an older one is kept and runs next, and a `.queued` marker left without its request is cleared instead of hiding a finished result. A sandboxed probe never calls host checks dead from its own PID view; the daemon's probe settles that. A detached check runs only after its launcher's explicit go, or once `checks.pid` names it, so a launcher that dies before recording it starts nothing.
