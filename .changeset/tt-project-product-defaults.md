---
"tt-project": patch
---

Product defaults teams can override. `delivery.drafts_during_freeze` (on by default): draft PRs need no one's word even under a code freeze; off, the coordinator asks before a draft PR under a freeze and the ask gate lets that ask through. The desktop notifier install and local view tunnels now ask once per user and keep the answer (`ttp consent [notifier|tunnel] [yes|no]`, `~/.tt-project/consent.json`); a no is never re-asked. New charter Policies line `- Change windows: none`: a team that lists its windows there gets a coordinator rule that schedules disruptive work into the next window (`start_after`/`start_when`) and never lets an `idle_capacity` event move it outside one.
