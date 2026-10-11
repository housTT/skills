---
"tt-project": patch
---

`tt-project`: the review-area cap no longer counts a review whose hand-off says
`review_failure: check_only`. Such a review found no problem in the code, only a missing or
inapplicable check, so it no longer uses up the area's automatic fix and re-review rounds, and
check-only failures alone never stop automatic fixes.
