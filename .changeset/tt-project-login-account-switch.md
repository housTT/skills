---
"tt-project": patch
---

When a provider's login comes back after a logout on another account than before it, nothing new starts until one more login check (about a minute), so a user still switching accounts can finish. If it is still another account then, work starts, the "logged in again" message names the change (account kind only, never an email address) and a `login_account_changed` event records it. The same account starts work at once, as before.
