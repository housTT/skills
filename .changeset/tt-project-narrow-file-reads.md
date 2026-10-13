---
"tt-project": patch
---

`tt-project`: Workers are told to find code with `grep -n` first, read only the ranges they need and not
re-read a range they already have. The worker hook adds a reminder (never a refusal) when a Bash command
would print more than about 300 lines of one file (`sed -n` with a big range, `cat` of a large file).
`ttp stats --tools` reports the runs' tool output by category, with the share that is file reads.
