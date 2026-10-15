---
"tt-project": patch
"tt-skills": patch
---

Failure messages for a `ttp detach --check` probe (until_probe, end_when, `when:` holds, machine
change until-probes) now show the job's exit code (`job rc=N`), not the probe's own exit 4; when it
cannot be read they say `probe exit 4`.
