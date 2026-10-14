---
"tt-project": patch
---

Obligation records in a run's file are filed once even when the run ages out of the filing window and comes back because its task is re-queued: the read offset of a run that filed records is kept instead of dropped, and offsets dropped by earlier versions do not replay the records filed inside the window. No more duplicate evidence entries or spurious 'obligation record refused' lines.
