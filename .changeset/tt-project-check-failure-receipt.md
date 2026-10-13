---
"tt-project": patch
---

Every failed `ttp checks` run now writes `check_failure.json`, including a mandatory selector that refuses the exact head in its own words, any failing check, a run where no check applied and a harness run. Only a missing target or a selector coverage gap for the tested head can still open a check gap; other receipts (kind `check_failed`, `scope_error`, `none_apply`, `harness_skipped`) and harness runs never do. `ttp note --to tt-project`, with no project of that name on the machine, files an `upstream: ...` note for tt-project's maintainers in the upstream inbox instead of being refused as an unknown project.
