---
"tt-project": patch
---

User-facing times (notify and alert texts, `ttp status`, the daily review, after-push and deploy reports, observation and hold texts, progress notes) now show the project's home zone with its abbreviation, for example `20:04 PDT`, instead of UTC or `Z`; machine logs and data files stay UTC. The worker, coordinator and daily-review prompts carry the same rule for text models write for the user. `ttp lock` records its "waiting for" progress note before a give-up, so a wait that gives up on its first pass under load still shows as a wait, not a stall.
