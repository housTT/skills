# Time to recover

`ttp ttr [<name>] [<alias>] [--days N] [--json]`, model-free: per machine the incidents open now or ended in the window (default 30 d), the median, p90 and max time to recover of the ended ones, the open ones and the last one, times in the home zone.
The daemon records an incident (every 60 s, even while paused; heal checks do not run then, so a heal report older than its check's period plus 5 min is ignored while paused) on each machine the charter's Resources name, the project owns or falls back for, or its heal checks name: it starts when one of its heal checks reports the machine down or held, a co-tenant's condition on it is live, or a known outage (`ttp machines outage`) is recorded, from the earliest, and ends at the first tick none says so.
Rows are only added and each end is written once, with one open incident per machine, so a daemon restart or reboot mid-incident neither loses nor duplicates one; a recovery while the daemon is stopped is recorded at its next tick.
The daily review gets one line per machine with an incident in the last 7 d.
A box that recovers slowly: give it a heal check with a fix, or a recovery owner (`ttp machines set <alias> --owner`)
