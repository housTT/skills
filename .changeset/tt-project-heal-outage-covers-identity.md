---
"tt-project": patch
---

`tt-project`: A known machine outage recorded with `--covers` (host, power, network, tray, chip, fabric, or now a service's name) keeps heal checks quiet only for the results it explains, by one rule: a covered word in the check's name, resource or unit, never in its output; with host, power or network, also a run that cannot reach the box (exit 75 or 255 without a `ttp-heal: not-outage` line). A health check that reports a lost chip under an outage covering one service now escalates, even when its output also names that service. A result the outage does not explain keeps the check's current episode, so its grace is not restarted. Known gap: a check named after a covered part stays quiet under that outage even when it also fails for another reason. An outage without `--covers` still quiets every check on the machine.
