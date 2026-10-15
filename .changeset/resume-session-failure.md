---
"tt-project": patch
---
A pytest check whose session failed though every test passed (a coverage floor, a plugin failing at session end) no longer leaves a resume record, so a later resumed batch reruns it instead of recording a pass.
