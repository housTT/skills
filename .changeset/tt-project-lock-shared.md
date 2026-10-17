---
"tt-project": patch
---

`ttp lock --shared <resource> -- <cmd>` holds a resource in read mode: shared holders run together, while exclusive holders (a plain `ttp lock`, or an exclusive task's whole-run hold) and shared ones exclude each other, and a queued exclusive waiter is not jumped by later shared ones. `ttp lock --probe --shared` answers for a shared taker, and wait probes and lock-probe pruning accept it. Not for a device lock: `--shared` on one exits 2 and runs nothing, and its probe answers as the plain one. A nested `ttp lock` of a lock its enclosing one holds in the same or a stronger mode just runs, saying so; an exclusive one inside a shared hold exits 2 at once. Shared holds go to the activity log as mode `shared`. The rules are in `ttp lock --help` and the tt-project-harness skill; the worker prompt is unchanged, for its size caps.
