---
"tt-project": patch
---

`ttp status` no longer reports a live daemon as NOT RUNNING from a sandboxed worker (own pid namespace or hostname): when the pid probe fails or it runs in a worker, a heartbeat under 2 min old reports 'running (heartbeat Ns ago)', an older one 'status unknown from this sandbox', and only a stale one NOT RUNNING.
