---
"tt-project": patch
---

The daemon checks the hosts of every waiting probe that exited 255 in one tick together, in parallel, so a network outage holds the tick for one ssh ConnectTimeout instead of one per probe. Each host is still asked at most once a minute, and what counts as a network outage or a broken probe is unchanged.
