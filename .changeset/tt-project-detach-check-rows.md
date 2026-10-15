---
"tt-project": patch
---

`tt-project`: `ttp detach --check` (local or `--host`) takes `--rows <file> --after <n>`: it exits 0
once that file has more than n lines, once a job ended, or once its driver is dead (pid gone, or the
host's boot id changed after a reboot or power cycle), else 1; ssh failing stays 255. A wait that
tests only an end marker over ssh, with no dead-driver check, is flagged by `ttp task set-when`, the
coordinator's start_when checks and the stale-wait event, pointing to the new option. The worker
prompt and docs say so.
