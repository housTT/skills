---
"tt-project": patch
---

`tt-project`: pr-watch raises no `pr_changed` event when only an open PR's CI moved (passing, pending,
failing and back). Each CI outcome is already reported once as `pr_findings` or `pr_clean`, so these
flips only woke the coordinator for nothing. A change of state, draft, mergeable, review decision or
review activity still raises `pr_changed`.
