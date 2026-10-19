---
"tt-project": patch
---

`tt-project`: the desktop notifier measures the gap that restarts a host-down streak from the last failed poll's end to the next poll's start, so a poll slowed by many unreachable hosts no longer counts as a gap and the host-down alarm still fires once
