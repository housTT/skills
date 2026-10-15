---
"tt-project": patch
---

Host confirmation of an exit-255 probe runs on daemon threads, so a running ssh check never delays daemon exit. Machine-change `--until-probe` probes now go through the same probe reaping as end probes: a 255 is "not yet" only once ssh cannot reach the host it names, and a broken probe (other exit codes, or a 255 whose host answers) is logged once.
