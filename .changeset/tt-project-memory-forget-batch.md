---
"tt-project": patch
---

`tt-project`: `ttp memory <name> --forget` takes several entries in one call (`--forget a b c`, or the
flag repeated) and retires them with one MEMORY.md rewrite and one harness commit instead of one per
entry. A name it cannot retire (unknown, ambiguous, or standing without `--why`) is reported and the
command exits non-zero; the others are still retired.
