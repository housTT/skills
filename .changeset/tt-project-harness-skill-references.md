---
"tt-project": patch
---

The tt-project-harness skill's ten longest table rows (Watchers, Self-healing checks, Model-free wait endings, Stock goal checks and six more, up to 7.7k characters each) now live in `references/<topic>.md` next to its SKILL.md, one sentence per line, with a one-line summary and a link left in the table. SKILL.md drops from 42,200 to about 19,700 bytes, so workers read less and concurrent edits to one row no longer conflict on one giant line. No fact was reworded. `ttp setup` already copies whole skill folders, so the references ship with it. A test now fails if any SKILL.md line in the plugin passes 1,000 characters or a reference file is not linked.
