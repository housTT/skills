---
"tt-project": patch
---

`tt-project`: Jev's pause after out-of-funds (1 h) or a busy reply (60 s) now holds for the whole daemon. Each config reload and each schedule run built a new Jev and dropped the pause, so they called Jev again while it was paused.
