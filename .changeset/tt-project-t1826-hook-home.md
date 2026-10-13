---
"tt-project": patch
---

Commands the daemon runs for a project (a hand-off's `on_pass_cmd`, `retry_when` and `start_when` probes, schedules, heal checks, `after_push`) get the owner's HOME: an inherited HOME that is missing, not this user's or inside the project's harness folder is replaced with the home from the password database, and USER and LOGNAME are set when missing. They still run from the project root. An `on_pass_cmd` runs only once the run that named it has ended. A failed `on_pass_cmd` logs its exit, output tail and the HOME and cwd it ran with in `daemon.log` and in the task's wake.
