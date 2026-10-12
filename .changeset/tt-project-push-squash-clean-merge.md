---
"tt-project": patch
---

`ttp push` and the push queue land a branch with merges whose rebase conflicts only because a commit after a merge edits lines the merge resolved: when its merge with the target is clean, they land one commit on the target whose tree is that merge. Its body lists the branch's commits, each also as a `Ttp-Replayed-From` trailer, so `ttp landed` finds them. A merge that conflicts too is still refused as a conflict.
