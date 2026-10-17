---
"tt-project": patch
---

pr-watch no longer makes the GraphQL review-comments read for closed or merged PRs, saving one API call
per poll for each; their recorded activity is kept.
