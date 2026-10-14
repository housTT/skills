---
"tt-project": patch
---

A failed `ttp checks` run now lists the applicable checks it never ran as `not_executed`, in both `check_failure.json` and `checks.json`. A mandatory selector that refuses the exact head leaves every check after it marked not executed, so the receipt never reads as those suites having run. The refusal itself and the mandatory checks are unchanged, and a passing run's `checks.json` is unchanged.
