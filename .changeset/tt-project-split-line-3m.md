---
"tt-project": patch
---

The standard tier's split line (`budget.split_reread_tokens.standard`) is 3 M re-read tokens, down from 4 M, which reached only a few standard runs. When the split steer fires, the run's note records the time and the re-read at that point, and the run's end adds how it handed off. `ttp stats` prints a line for steered runs: how many, what they re-read after the steer (average and max) and their hand-offs.
