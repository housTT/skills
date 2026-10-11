---
"tt-project": patch
---

`tt-project`: A provider's budget gate going red, or a usage or rate limit, is now a low-severity
status line in the feed that clears itself, never a "needs you" item or a chat alert asking the user.
Only an account out of credits or funds (or a spend limit only the user can raise) is still a high
alert. The one-line budget line in the web app header and `ttp status` now also names every provider
holding ready work, core or not, e.g. `..., codex 7d 93% holding 1 task`.
