---
"tt-project": patch
---

Heal checks can tell a box that is powered off or rebooting from this project's own route problem. Set `ping_host` (the box) and `ping_peer` (another host on the same network) together: a check that exits 255 while the box does not answer ping and the peer does counts as the box down (an outage, no fix attempt) instead of a route self-fix. Everything else keeps the route rule; a silent box with no peer answering, other exit codes and ping that cannot tell stay a route problem.
