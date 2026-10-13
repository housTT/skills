---
"tt-project": patch
---

A host-side `ttp upgrade` of a paused project now takes the template merge over from a harness task past its takeover time (named by `--take-over-from`, or blocked or idle past 6 h), as for any other stuck owner: the task is cancelled as superseded, the finish-lock generation goes up, a clean merge lands, and a conflict queues the merge task again, held by the pause. The pause is never lifted. `ttp deployed`, `ttp status` and the after-push deploy name the pause as the reason a paused project is behind instead of a takeover deadline that kept sliding; its pending upgrade stays a low note, not a page.
