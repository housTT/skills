---
"tt-project": patch
---

`ttp check-plan` resolves the repository top level, so it works from a subdirectory instead of failing with 66. The README notes that a selected command exiting 65, 66 or 20 is told apart from a check-plan error only by its `check-plan:` stderr line.
