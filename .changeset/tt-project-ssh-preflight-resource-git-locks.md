---
"tt-project": patch
---

New `ttp ssh-preflight <host|@resource>`: exits 0 once the host answers over ssh, 255 while not. `@<resource>` looks the host up in new config `device.hosts` ({resource: host}, else a device runner's host) each time it runs, so a waiting task's probe follows a resource moved to another box; an unknown resource exits 2. Workers whose project maps resources are told to probe by resource name. The daemon also removes `.git/*.lock` files in the harness repository that no process holds once they are older than `watchdog.stale_git_lock_s` (600 s; 0: off), and logs each removal: a lock left by a killed git command no longer blocks harness commits.
