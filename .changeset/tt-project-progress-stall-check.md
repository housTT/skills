---
"tt-project": patch
---

`tt-project`: a model-free progress and stall check in the daemon tick raises a keyed `no_progress` or `stall` event when queued or running work yields no result past its threshold, a probe keeps saying not yet, runs keep failing the same way, or a schedule keeps failing the same way; it flags a wait on what the task itself fixes. It shares `progress.py` and one reader with `ttp progress report` (rotated reports still count), leaves refusal streaks to that report check and own-daily-cap skips to the schedule-budget observation, and clears a key only after it stays healthy for 3 checks spanning 30 min, so a key that flaps does not wake the coordinator again. A queued task's clock starts when it became ready, not when it was created, so a task released after a long wait behind a dependency, start time or pause is not reported stuck at once. Settings: `coordinator.progress_check`.
