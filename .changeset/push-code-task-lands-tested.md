---
"tt-project": patch
---
`ttp push` (delivery to the push branch) from a code task's worker now lands only when `ttp checks` passed on its committed HEAD, besides its spec asking to land; `--land` overrides both and is logged. The worker prompt names `ttp push --own --detach` for publishing a task branch and keeps plain `ttp push` for reviews, the push queue and code tasks told to land.
