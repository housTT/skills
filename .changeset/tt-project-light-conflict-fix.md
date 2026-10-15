---
"tt-project": patch
---

When the push queue sends an approved head back with a textual conflict, its review runs again as a light conflict fix with a short prompt (rebase keeping both sides' intents, focused tests, range-diff, approve the new head) instead of a full review attempt. It runs at standard when the batch's merged checks failed, the rebase stopped for another reason, both sides changed the same Python function or test, or a light fix did not approve a new head; that retry is immediate and starts no coordinator turn. Conflict fixes no longer count toward the review's attempt cap; their own cap is how many approvals the queue sent back. `ttp push --queue` shows the last 24 h of conflict fixes and their cost.
