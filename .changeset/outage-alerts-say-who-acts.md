---
"tt-project": patch
---

Outage messages share one wording (`outagemsg`): machine pages, the daemon's outage alerts, heal checks'
outage and untenanted-hold alerts, network outage alerts and `ttp machines status` now state when the outage
began (home zone), how long it has lasted so far, who is acting now (owner, fallback and why the owner was
passed over, or "nobody: escalating"), the tracked task and its due time when one is recorded, what was
tried and the next step. A bare "nothing was changed" or "a note was sent" outage alert gets the actor
added, and a coordinator notify saying only that is refused with what to say instead.
