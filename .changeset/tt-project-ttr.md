---
"tt-project": patch
---

New `ttp ttr [<project>] [<alias>] [--days N] [--json]`: time to recover per machine. The daemon records machine incidents model-free (from its heal checks' machine reports, co-tenants' down or held conditions and known outages) in an append-only table with one open incident per machine, so restarts and reboots neither lose nor duplicate one. The report gives per machine the incidents, median, p90 and max TTR, the open ones and the last one in the home zone; the daily review shows one line per machine.
