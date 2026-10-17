---
"tt-project": patch
---

Only outage paths get the "Acting now" wording: a daemon alert whose key starts with `outage:`,
`machine:`, `heal:`, `heal-hold:` or `network:` (or that passes `outage=True`), and a coordinator notify
with `outage: true`. Alerts are no longer classed as outages by words such as "held", "down" or "owner",
so a root-checkout alert or "Push held: nothing was changed." goes out unchanged and such a notify is
no longer refused.
