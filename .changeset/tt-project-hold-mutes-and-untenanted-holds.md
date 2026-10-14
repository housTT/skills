---
"tt-project": patch
---

A held device can no longer stay silent indefinitely. `ttp mute list <project>` shows a project's observation mutes and `ttp mute end <project> <source> <match>` ends one without a coordinator turn (its end summary names who ended it). An `observation_mute` whose match or covered conditions name a device hold asks after at most 6 h, whatever `escalate_after_h` says (0 included); the action's ack says so. A heal check that prints `ttp-heal: untenanted-hold` (the broker preset does while `hold_field` is set and its new optional `holder_field` is empty) pages once the hold outlasts the project's `heal.untenanted_hold_s` (4 h), even under a known machine outage, a check standing down with 75, a known fault or a mute (`heal-hold:<name>` high alert, or one high observation with `escalate: coordinator`); only a pause of its resource the user set or that names the check keeps it quiet.
