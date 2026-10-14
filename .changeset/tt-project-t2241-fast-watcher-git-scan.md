---
"tt-project": patch
---

`tt-project`: The harness skill's rule for watchers that workers run at task start or hand-off now
says they must finish in seconds in a big checkout: `git status --untracked-files=no
--ignore-submodules=all` (or `--untracked-files=normal` when untracked files matter, never `all`, so
a virtualenv, data or build folder is not walked file by file), a timeout per git or network step,
and a `skipped: <step> timed out` error line instead of a hang.
