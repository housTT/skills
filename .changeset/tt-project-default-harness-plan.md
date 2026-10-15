---
"tt-project": patch
---

`ttp checks --purpose harness` no longer fails with "delivery.push_checks has no purpose harness check" in a project that configures none: it runs a built-in default harness plan, a `git diff --check` of the head (leaving out `*.log`) and pytest of the harness's own `runtime/tests` and `bin/tests` where they exist (a missing folder is reported skipped, never passed). A configured purpose harness check still replaces it.
