---
"tt-project": patch
---

Coordinator digest: the machine ledger's seen lines are keyed on the ledger alone, so a prompt-snapshot refresh no longer shows the whole shared-machines ledger (and breaks the cache); the turn still shows only new or reworded lines. A whole view (after a machine event, or with no previous turn) is capped: conditions, outages and the recovery-owner gap in full, at most 10 open change lines (overdue first, then the project's own), and one line counting the rest (`ttp machines change list`).
