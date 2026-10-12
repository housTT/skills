---
"tt-project": patch
---

Detached jobs are checked for using what they asked for. `ttp detach` records the request (`--cores`, `--devices`, `--mem-gb`, else `-j N`-style flags, or `OMP_NUM_THREADS`/`MAKEFLAGS` only when the command sets them or they differ from the daemon's environment; nothing for work sent over ssh), and the daemon samples the CPU time of the job's own process tree each minute, model-free (Linux /proc, macOS ps; open device files from the new `device.nodes` globs on Linux). Under a quarter of the requested cores, or a held device no job process opened, for 15 minutes raises one `job_underuse` event per stretch and brings the task's next check within 30 minutes (`waiting.job_use`); if use recovers first, that check follows the probe as usual. `ttp status` and the web app show requested vs used per job, or "unmeasured" where sampling is unavailable.
