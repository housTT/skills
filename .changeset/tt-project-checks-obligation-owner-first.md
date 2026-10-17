---
"tt-project": patch
---

A task that is a required owner of an open obligation now goes first among its own project's waiters for a
host-wide checks slot (`ttp checks` and `ttp push --own`), instead of waiting behind every other priority-1
task. `ttp status` and the wait message mark it. It ends once the obligation has its receipt or is dropped.
Turns between projects are unchanged, and push-branch checks keep their own slot beyond the cap.
