---
"tt-project": patch
---

A budget red for the runaway guard or a dollar cap alerts once per episode instead of once per crossing: the episode ends only once the measured value falls below 80% of its limit, and the same reason red again within an hour of that end posts no new alert. Gating is unchanged: new work still waits at the limit. A dollar cap of 0 counts as unset in the budget text: a reached cap names only the caps set, and a plan that stopped reporting its windows (or a login on another account) no longer mentions dollar caps when none is set. The plan coordinator-turn guard (`budget.hourly_coordinator_turns`) now defaults to 80% of `coordinator.max_turns_per_hour` instead of 40, so it can fire before that limit holds the turns back.
