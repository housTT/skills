---
"tt-project": patch
---

`tt-project`: a coordinator turn that goes over `coordinator.turn_budget_usd` is never retried as is.
Each item of the failed turn gets its own record: a batch is halved down to single items at the
configured cap; a single item runs once more alone at the new `coordinator.turn_budget_ceiling_usd`
(0: twice the cap, never above the hourly coordinator limit or the dollar caps) when the caps leave
room; one that fails even then is parked, stays pending and is named in one normal alert with every
other parked item, and runs again only once the cap or ceiling rose above what it went over. Other
items go on, and their turns never touch its record.

Accepted gaps: a split is by item count, not size, so a heavy item in a large batch costs a few
halvings before it runs alone; a raise waiting for room under the hourly limit waits as long as
spend keeps it out; the parked set the alert names is refreshed when the daemon plans a turn, so a
setting raised while no turn can start clears the alert on the next plan.
