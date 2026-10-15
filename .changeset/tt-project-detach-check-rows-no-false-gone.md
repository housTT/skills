---
"tt-project": patch
---

`tt-project`: `ttp detach --check --rows <file> --after <n>` (local or `--host`) no longer reads a
path with no detached job recorded (no .rc, .pid or entry, e.g. a run another tool started) as a
dead driver: only a new row in the file or a real end marker wakes it, and the local check leaves no
`.lost` file there. `--rows`, `--after` and `--host` given after the paths (or as `--opt=value`) are
read as options, not as more paths; an unknown option is refused.
