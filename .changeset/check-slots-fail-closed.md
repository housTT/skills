---
"tt-project": patch
---

Workers' sandboxes may now write `~/.tt-project/check-slots`, so their `ttp checks` count toward the
host-wide checks cap. Where that directory cannot be written, `ttp checks` stops with an error naming
it (exit 2) and `ttp push` and the push queue do not push, instead of running the checks uncapped.
