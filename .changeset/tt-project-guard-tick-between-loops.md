---
"tt-project": patch
---

The daemon tick now also guards the calls between its two step loops (meter refresh, gate update, ask and mute expiry, mute-end checks, watcher issue sweep, Jev review, the settle check) and the outbound delivery while paused. One that raises is logged and alerted as `tick-step:<name>` like any step, and dispatch, Slack polling, pushes and outbound delivery still run in that tick.
