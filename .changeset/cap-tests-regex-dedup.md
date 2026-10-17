---
"tt-project": patch
---

The default prompt size cap test pattern now matches only names ending `_stays_small`, `_under_its_cap` or
`_size`, so logic tests that mention prompt caps no longer count. A cap failure lists an overshoot once even
when its `assert N <= C` line appears both prepended and in the output tail, keeping the entry with a test name.
