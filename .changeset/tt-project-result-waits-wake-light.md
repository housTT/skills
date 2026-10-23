---
"tt-project": patch
---

`tt-project`: a wait on `ttp checks --result` or `ttp push --result` wakes at light whatever `wake_tier` it asked for, as ack-wait probes do; failed checks and a push that did not land (read model-free from its marker) wake the task at its own tier. The worker prompt says to leave `wake_tier` out for these waits.
