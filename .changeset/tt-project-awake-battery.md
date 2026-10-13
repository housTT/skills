---
"tt-project": patch
---

`tt-project`: On a Mac running on battery with work queued, status and the web app now say that
keep-awake is not held because it holds only on AC power, and a lid closed on battery says so too.
The daily `host_slept` alert names the cause recorded before the sleep (battery power, keep-awake
off by config, or a closed lid) instead of always implying keep-awake was on.
