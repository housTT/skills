---
"tt-project": patch
---

On Linux with a systemd service, the daemon checks linger at start and daily, and raises one `linger-off`
alert (it clears itself) while a reboot would leave it down until the next login. A daemon back more than
`daemon.down_notice_s` (1 h) after its last heartbeat posts one message with the downtime, its cause, the
schedule runs missed and the runs cut short, merged into the reboot notice when both apply.
