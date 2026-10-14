---
"tt-project": patch
---

Command schedules and heal checks can page another project on the same machine without a model run. A watcher's JSON line may carry `"to": "<project>"`, or `"to": "@machine-owner"` with `"machine"` (a known outage's owner project, else the machine's recovery owner, else its fallback): the daemon files it as a note to that project's coordinator instead of an observation here, once per `key` (else per text) until the watcher stops reporting it, again after `rewake_after_h` or on every run with `"repeat": true`, at most 10 notes a run. A heal block's `to` sends its final escalation and its unknown-stretch message to that project the same way, once per episode. An unknown target is logged and the line or escalation is handled as before.
