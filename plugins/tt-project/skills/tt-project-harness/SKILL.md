---
name: tt-project-harness
description: "Improve a tt-project's own harness — its coordinator and worker prompts, schedules, watchers, budget settings and web app — from measured friction, and take upstream template updates. Use for harness tasks, daily reviews, or when a project wastes money or time or needs the user too often."
---

# tt-project: harness

## Purpose

- A project's harness is `<root>/tt-project/harness/`, its own git repo.
- It starts from the tt-project template, then adapts to this project.
- This skill changes how the project runs. It never changes the project's goals.

## When to Invoke

- A task of kind `harness`, or a daily review proposing harness changes.
- The user says the project is slow, noisy, expensive, or asks too much.

## Evidence first

| Question | Look at |
|---|---|
| Where does money go? | `ttp status <name> --json` → `budget.top_sources_7d`, `runs` |
| Which runs made no progress? | failed/stalled runs, tasks with many attempts |
| Is the coordinator thrashing? | coordinator turns per hour, rejected actions events |
| Is the user asked too often? | `ask` messages in the last week |
| Is recurring work earning its cost? | `schedules` → `cost_7d` vs what it produced |

## Levers (cheapest first)

| Lever | File |
|---|---|
| Coordinator rules | `prompts/coordinator.md` |
| Worker contract, per-kind rules | `prompts/worker.md`, `prompts/kind-*.md` |
| Tiers, caps, debounce, idle wake, resources | `project.json` (defaults: `runtime/ttp/project.py`) |
| Task pin verification | `runner.pin_verify` in `project.json`: `lenient` (default) lets an explicitly model/effort-pinned task run if its provider cannot report the resolved session before a model turn; `strict` refuses it. Set with `ttp config <name> runner.pin_verify strict`. `task_add` / `task_update` accept `pin_verify: strict` or `lenient` to override the project; `""` clears the override, and it cannot change while the task runs. Reviews, fixes and continuations inherit a pinned task's override. A reported model/effort mismatch always refuses. A strict unverifiable pin blocks the task with the reason, starts no model turn, spends no attempt and does not automatically retry; restore verification or explicitly change the policy, then requeue. Unpinned tasks are unaffected. Each check records `session.verification` (`verified`, `mismatched`, `unverifiable`), `pin_verify`, timestamp and resolved fields or error in `run.json` and the run's note. Providers without a session query are unverifiable. |
| Recurring work | `schedules.json` when the harness has it (the daemon applies edits; `ttp schedules <name> --export --yes` moves them there once and commits; to only read one, `ttp schedules <name> show <schedule>`), else coordinator `schedule_set` or the web app's Recurring pane |
| Watchers | `kind: command` schedules; scripts under `watchers/` in the harness. One JSON line per observation: `{"text", "severity", "repeat", "whole", "key"}`; `"repeat": true` wakes the coordinator on every new occurrence, otherwise a known one wakes again after `screen.rewake_after_h` quiet. Write a line as `<subject>: <item>; <item>`: each item is one issue (counts masked), an item may start with `now`, `still:`, `changed:` or `cleared:`, and `cleared: <item>` closes it. A JSON line with `"whole": true` is one observation instead: one issue for all the text after its subject, never split at `; ` (a long title is truncated); a leading `cleared:` closes it. An optional `"key"` (a short stable string) is the issue's identity instead of its text, so a changed text under the same key updates that issue and opens no new one. On a keyed line, text after the subject that starts with the word `cleared` (any case, colon optional: `cleared: X`, `cleared X`, `Cleared by ops`) is a clear: it closes the open issue under that key, if there is one, and never opens one, so `fleet gate: cleared: X` with key `X:cleared` raises nothing. Put the state after the item instead (`fleet gate: X cleared`). A leading `now`, `still` or `changed` is stripped and the keyed issue opens or updates as usual. Give every line whose text changes from run to run a key naming the condition (`"key": "box-a down"`): a varying reason, a duration (`down for 0.7h`), a count or a long line with `; ` in it otherwise opens a new issue per variant or fragment. A watcher that opens more than `screen.same_subject_warn` (3; 0 off) issues on one subject within 24 h gets a daemon log line and a note in the digest and the daily review, with no wake of its own: fix its output. A multi-line observation without a key is keyed by its first line: make it a stable heading and list the changing items below it. An info line (`"severity": "info"`) takes the same path as any other: its `cleared:` items close issues and it replaces older receipts of the same subject. Known gap: info lines are not suppressed, so an item first seen at info is still recorded as a quiet issue (it wakes once a line rates it higher). A run that prints nothing closes the watcher's open issues, and an issue not seen for 24 h (or 3 periods of a slower watcher) closes; a closed one seen again reopens and wakes at its severity. Exception: a keyed issue that a run printing nothing closed, sent again under its key within `screen.reraise_window_h` (6; 0 or null off) of that close at the same or a lower severity, only counts: it stays closed and wakes nobody; a higher severity reopens and wakes, and a `"repeat": true` line always wakes. An event that must stay until its owner acknowledges it (a receipt): print it every run, with its identity as a prefixed token (`evidence_<hash>`: bare hashes and numbers are masked), until the owner acknowledges it, and set `rewake_after_h` null so an unchanged one never wakes again; a changed token wakes. A receipt source that may miss runs or report several subjects also sets `issue_lifecycle` `explicit_clear`: its items then never close by time (downtime included); one closes when the owner acknowledges it (`cleared: <item>`, or a run that prints nothing) or when a run reports a different item for the same subject (the changed outcome wakes; a later return to the old one wakes too). Exit codes and errors: exit 0 or 1 is a normal run (1 is how a checker says it found something), so every line it prints is a report under `watcher:<name>`, also a line whose text says error or failed. When the watcher itself could not check (a source it reads is missing, a call failed), print that as a JSON line with `"error": true` (the JSON boolean; the string `"true"` is a report), at any exit code. Any other exit code is a failed run: with no output it is one error; lines it printed still count (reports, or errors where marked) and the exit code is one error more. A watcher's own failures (timeout, failed exit code, or a line with `"error": true`) are errors under `watcher-error:<name>`, never receipts: they close after the usual quiet time or at the next run at exit 0 or 1 that no longer reports them, so the same failure later wakes again. Mark them: an unmarked failure line is a report, which stays open while the watcher prints other lines, so the same failure coming back after a good run wakes nobody. With a Jev key, Jev rates each new observation; each call is logged with its cost and the coordinator wake it skipped, and a Jev use that saves nothing over `jev.window_days` (at least `jev.min_calls` calls) switches itself off once, reported, as does one whose last `jev.idle_calls` (20) calls changed none of the rules' decisions; `jev.uses.<use>` = `on`/`off` forces it (uses: `screen`; `effort`, which may start a short-lookup task the coordinator queued at standard at light) |
| Self-healing checks | A `kind: command` schedule with a `heal` block in `payload` (`schedule_set` field `heal`): `check` (shell: exit 0 healthy, 1 unhealthy, 75 or 255 unknown, which never fixes; a check without `outage` unknown for `unknown_cap_s` (3600, 0 off) without a break tells `escalate` once, with its last output and blocker: a normal alert `heal-unknown:<name>` or one high coordinator observation, until a healthy or unhealthy result starts a new stretch) or a `preset` (`systemd`: `unit`, `user`, `host`, unit active and NRestarts not rising, default fix a restart; `http`: `url`, `expect` status codes; `broker`: `status_command` printing JSON with no `hold_field` set and every `auto_fields` flag on; optional `expected_count` (JSON `count` must equal it) and `max_recent_failures` (JSON `recent_failures` at most it), a missing or invalid field reads unknown), `fix` (optional), `resource` (the fix runs under `ttp lock`; busy or paused defers it uncounted, a lock busy (not paused) past `window_h` escalates, a fix waiting on a pause past `unknown_cap_s` says so once (who set it, what lifting it allows): a user's pause one low line and no ask, any other one high coordinator observation; the pause is never lifted, and a fix that itself exits 75 counts), `grace_s` (300), `settle_s` (60), `max_fixes` (3) per `window_h` (1), `timeout_s` (120, capped at 240 so a step never outlasts the daemon's watchdog; a timeout kills the whole process group). The daemon runs it with no model: unhealthy past grace runs the fix and rechecks after settle; a fix that worked is one low feed line and a digest record, with no coordinator wake. A failed fix, a used-up cap or no fix queues one priority-1 self-fix task per check (label `heal:<name>`) carrying the check and fix output; only when that task fails, blocks or ends with the check still failing does a high outage alert `heal:<name>` go up, and it clears itself once the check passes. `escalate` (`user` by default, or the project's `heal.escalate` config) picks who gets that step: `coordinator` sends no user alert and instead queues one high observation per unhealthy episode for the coordinator (fingerprint `heal:<name>:<since>`, never repeated within the episode). `known_fault` (a reason) keeps a known single fault quiet, but with `outage: true` (a whole box or resource that serves nothing) it never stays quiet past `grace_s` or 60 min, whichever is first, also while its check stays unknown (an unreachable box): that escalates without running the fix and clears on the next healthy result; mutes never fold it. A check that finds a problem while the box is up (a non-outage finding) exits non-zero and prints a line `ttp-heal: not-outage`: that run counts as plain unhealthy as if `outage` were off (its own `grace_s`, the fix, `known_fault`, the ordinary self-fix task, no outage wording and no machine ledger `down` report); a check that never prints the line keeps the outage behaviour. An existing user alert closes when escalation moves to `coordinator` (including the project default) or `known_fault` covers the current failure; it records the reason without claiming the check passed. `ttp alerts <project> --ack <message-id> --why "<reason>"` acknowledges a keyed alert episode: closed if over, else kept open in the feed, its repeats quiet until it clears; a live outage is refused; history and the original acknowledgement are kept on retries; unknown ids are refused. An outage check whose `resource` is paused is not fixed, alerted or reported to the machine ledger: silent for the whole pause when the user set it or its reason names the check, otherwise for `heal.paused_quiet_s` (6 h) of the outage, after which the coordinator (never the user) is asked once per stretch whether the pause is still right; nothing here lifts a pause. `ttp heal list|test <check>` shows the checks and runs one dry (no fix); `ttp status` and the web app show `health: N ok, M fixed today, K failing`. |
| Box-clean gates | `tt-box-clean-probe` (in the harness's `bin/`, on every probe's PATH) answers "has this box been N hours without a device incident?" with no model, read-only: host uptime plus the tt-device-mcp broker's recent jobs (boots, broker resets, power-cycles, reboots, PCI rescans, health gates, hold starts or a device held degraded, failed broker jobs, tenant jobs the broker killed (or left `[MCP killed ...]` by a chip drop), found hung or that ran a reset), each dated by when it finished (a running job or a hold still on counts as now), in the host's local time as the broker writes it. Use it as a `start_when`, `retry_when` or resource-pause `end_when` instead of a daily light run that only checks: `tt-box-clean-probe --window-h 24` on the box itself, `ssh <box> python3 - --window-h 24 < "$TTP_PROJECT/harness/bin/tt-box-clean-probe"` for another. Exit 0 clean, 1 not yet (prints the newest incident and the earliest clean time), 255 cannot tell. With no broker installed it judges on uptime alone (`--require-broker`: 255 instead); `--broker-python` (env `TT_BROKER_PYTHON`), `--port`, `--limit`, `--jobs-file` (JSON instead of the broker). |
| Readiness probes | A `start_when` or `retry_when` that waits for a device or box checks it live and read-only (the broker's status, `tt-box-clean-probe`), never a bare marker file such as `test -f state/ready/<box>.READY`: the file outlives the box going down, so the task starts against a dead box. A marker that is used is removed by whatever writes it when the resource is paused, shows trouble or fails its probe. `task_add`/`task_update`, `ttp task add --start-when` and `ttp task set-when` flag (never refuse) a probe that only tests a readiness marker. |
| Push check outputs | `delivery.push_checks` receive a writable absolute `TTP_RUN_DIR`, never empty or `/`. Foreground `ttp checks` uses the worker's run directory; detached checks, detached pushes and push-queue batches get unique directories under project `state/check-runs/`, outside the checked worktree. Use `"$TTP_RUN_DIR/report.json"` for durable outputs. `checks.pid` and queue markers record `check_run_dir`; a restarted daemon rebuilds queued directories and a rebooted check invocation gets a fresh one. Outputs survive worker cleanup and completion; only detached checks' temporary scratch is reclaimed once its lifetime lock is free. Symlinked directories or locks are refused. Missing, invalid or unwritable output storage fails before checks; never skip a check or substitute `/`. Detached launches strip `TTP_PIDNS` and `TTP_CGROUP`. |
| Model-free wait endings | A waiting hand-off's `on_pass` (the final hand-off, recorded when the run's own `ttp checks --detach` pass) or `on_pass_cmd` (a command the daemon runs from the project root once `retry_when` passes and the run that named it has ended, with the owner's HOME, USER and PATH like every daemon-run hook (absolute paths preferred), killed after `waiting.on_pass_timeout_s`, 600 s; exit 0 with a `done` or `needs_review` hand-off written to `$TTP_RESULT` ends the task with no model run; a non-zero exit or timeout wakes it at its own tier, anything else as usual, with the command's output tail and the HOME and cwd it ran with, also in `daemon.log`). Runs ended this way show `run_status` `model-free`. |
| Setting holds | While `delivery.allow_protected_push_branch` is off, refuses the push branch and an open ask names it, the daemon (runtime `settinghold.py`) keeps every landing task on that ask (`waits:ask:<id>`, blocked_reason `setting hold: ...`), attempts untouched: queued ones before they start, and blocked ones a worker handed off naming the setting. A review with a push step runs as review only and hands off blocked naming the setting. All are released together once the setting is on or the ask is resolved; the digest shows one line per setting. A charter line granting pushes there (allowed, or no approval needed; the verbatim Brief counts as the user's word) while the setting is off and unasked makes the daemon raise one ask itself (blocking restriction, naming the setting), model-free, at setup or the first tick after a charter or delivery edit: once per push branch and charter wording, never per turn. That ask is only for a grant with no user word on record: see Charter grants. A coordinator config_set the daemon rejects names that open ask, and a memory_add in the same turn naming the rejected setting is marked `Not in effect` in the entry. A project-local copy of this logic is no longer needed. |
| Charter grants | The daemon (runtime `grants.py`) compares the charter's Brief and Policies with the live delivery settings, model-free, at setup and the first tick after a charter or delivery edit: the Policies `Auto-merge repositories:` line against `delivery.auto_merge_repos`, and a grant of pushes to the push branch against `delivery.push_allowed` and (for main, master or the remote's default) `delivery.allow_protected_push_branch`. A push grant the config lacks is applied only on the user's word on record (the Brief, or a Policies entry `CHARTER.history.md` logs with a user message), with one low alert and open asks about the setting closed. Auto-merge is report-only: the coordinator can write that charter line itself, so the daemon never writes `delivery.auto_merge_repos` (the coordinator sets it with `config_set` in the turn carrying the user's word). Everything else (any auto-merge mismatch, no user word, the charter also forbidding it, a setting set back after it was applied) is one keyed `grant_mismatch` event for the coordinator, never an ask; nothing the charter does not grant is ever set. |
| Charter guard | The coordinator's `charter_update` (runtime `coordinator.py`, `_reject_contradicting_append`) refuses added text that a standing Restrictions item would contradict, quoting the item and naming the forms that pass: a quoted resend, `both_hold` plus a companion edit for an exception, or `both_hold` (or the text saying it loosens no restriction) when both hold. A companion edit keeps the quoted item word for word and only adds a clause naming, by id, an exception that stands in a temporary or dated Restrictions section, with no object or action of its own, on the sentence that exception is about; the user's yes to that exception covers it in a later turn and it is checked against its own item only. Removing or really loosening an item still needs the user's word or `over`. A refusal that names such a fix raises an immediate `charter_retry` event, at most two per change. Raise `CHARTER_GUARD_VERSION` and its pin when the guard changes. |
| Obligations | `ttp obligation` (runtime `obligations.py`): a deliverable owed to the user, tracked on its owner tasks until a receipt. Each owner has a 2 h durable-progress clock; a milestone (`key`, absolute `due`, `evidence_required`, set by the coordinator's `obligation` action) raises one high `obligation_overdue` episode at its due time without its receipt (`receipt <id> "<evidence>" --milestone <key>`), whatever the heartbeat or other progress. Known gap: nothing checks that an owner can advance it, so an obligation whose owners all wait or stall is caught only by the 2 h progress rule (or a milestone falling due); a daemon-side coverage check was dropped for its false alarms on ordinary waits and review stacks. |
| Shared watchers | A watcher script that workers also run (at start or hand-off) takes a non-blocking lock (`flock -n` on a file under the harness state, or `ttp lock` with a short wait) and exits quietly if busy, re-checks the condition before it acts, and dedupes by condition key. |
| Web app | `runtime/ttp/web/` |
| Runtime behavior | `runtime/ttp/*.py` (last resort) |

## Rules

- One friction, one small commit, a one-line reason in the message.
- NEVER loosen a restriction, cap or notification policy without the user.
- Runtime edits: keep Python 3.9-compatible; run `python3 -m pytest -q runtime/tests` if present;
  (pytest from the harness root uses its `pytest.ini`, isolated from any enclosing repository's
  pytest config and `conftest.py`, with `runtime/` on the import path; add local paths to its `pythonpath`)
  then `ttp restart <name>`. It waits for the daemon to tick; if it does not, `runtime/` goes back
  to the last version that ran (a new commit) and the user is alerted.
- Measure after: same evidence, a day later. Revert what did not help.
- Change only this project's own harness. Never edit, or create a worktree or branch in, the
  tt-project plugin's source repository or another project's harness. Running `ttp setup` or
  `ttp upgrade <name>` to deploy a release to another project on this machine is not editing its
  harness; hand edits to its charter, memory, config, state or code are.
- A lesson every project would benefit from → an upstream note in the hand-off: a follow-up
  titled `upstream: …`.
- Notes reach the project that reads them even when its machine cannot reach this one (a laptop
  behind NAT): each daemon sends this machine's own notes on over ssh to `ttp upstream --receive`
  there, with the user's keys and known hosts and no new port. Notes go only where they are read;
  one for a project (`ttp note --to`) goes only to the machine that runs it. `ttp upstream
  --forward-status` shows each target's cursor, last success and last error; `ttp upstream
  --forward-to <aliases>|default|none` sets the targets. Received notes stay untrusted data.
- A `ttp note --to` past 4,000 characters (whitespace joined) is refused whole, never cut; a
  filed or queued one prints its stored length and a short hash. Put the request first. A hand-off
  `upstream:` note past the limit is cut with a marker saying how much was kept.
- Closing a note: the maintainers record `ttp upstream --resolve <fp> --fixed-in <version>`, or a
  code task's hand-off lists the note in `resolves` and the push queue records it with the version
  its batch pushed. A sandboxed `--resolve` or `--ack` is queued in the run's directory and
  recorded when the run ends. Work
  waiting on it hands off `waiting` with retry_when `ttp upstream --fixed <fp>` (0 once this
  project's running daemon has the fix). The daemon then tells the coordinator once, and the
  project closes the note with `ttp upstream --ack <fp> "<evidence>"`. With no ack a day after the
  resolution, it shows as "shipped, not confirmed". `ttp upstream --mine` lists this project's notes.

## Maintainer checklist (liveness, progress, deploys)

- Accept a deploy only through `ttp deployed`: every project's daemon reports the release by its
  own receipt. An installer exit, the files' version or a fresh heartbeat is not a deploy.
- Never accept "no tests collected" as a pass, and never blanket-skip a check. Fix where the
  check's applicability is declared; a related change that lacks its test still fails.
- Wait on an upstream fix with a probe (`ttp deployed`, a version check) as `retry_when`, never
  `blocked`. A hand-off is done only once the consumer acknowledged it and recovered.
- See "Liveness, progress and deploys" in `docs/design.md`.

## CPU-only runs never open a device

- The daemon sets `TTP_NO_DEVICE=1` for a task run that holds no device (not tagged `needs_device`,
  no `*-device` resource or device lock), and `0` for one that does. Hooks can honour it.
  `ttp lock` on a device resource sets it to `0` for its command, and `ttp devq` runners and jobs
  never inherit it: those are the sanctioned ways to a device.
- For a pytest repo whose tests open a device through fixtures, ship `ttp_no_device.py` from this
  skill's folder: copy it next to the repo's root `conftest.py` and add
  `pytest_plugins = ["ttp_no_device"]`. Under `TTP_NO_DEVICE=1` it deselects tests that use a device
  fixture (globs, default `*device`) or marker (none by default). Set the lists per repo with the
  `ttp_device_fixtures` / `ttp_device_markers` ini keys or the `TTP_DEVICE_FIXTURES` /
  `TTP_DEVICE_MARKERS` variables (comma-separated).
- Do not rely on `-m 'not device'` alone: a marker the repo does not define deselects nothing.

## Rebooting the harness's own host

A reboot ends every run in flight, and nothing lifts a plain pause afterwards: schedules and probes
do not run while the project is paused. A task that must reboot the host its harness runs on:

1. `ttp drain <name> --wait 240`: no new runs or pushes start; it exits 0 once none is in flight
   (the calling run does not count) and 1 at the timeout, with the project left paused. Running
   workers are never killed. Keep the wait inside one tool call (a few minutes). On exit 1 run it
   again in further tool calls of the same run, at most 3 times. Still not drained: `ttp resume
   <name>` and hand off `waiting` with a time-based `retry_when` (`test "$(date +%s)" -ge <epoch>`).
   Never leave a paused project with a waiting task: probes do not run while paused, so it never wakes.
   Drain also lists the project's `ttp detach` jobs still running on this host: the reboot ends them.
   `--jobs` waits for them too. A chain that only drives another machine belongs there instead
   (`ttp detach --remote`), where this host's reboot cannot end it.
2. `ttp pause <name> --until-reboot`: the daemon lifts this pause on its first tick after the host
   booted again and says so in the feed. A plain `ttp pause` or `ttp resume` replaces it.
3. Write the hand-off (`waiting`, with a `retry_when` that holds once the host is back, and no
   `survives_reboot`: a reboot then wakes the task at once), and start the reboot a little later
   (for example `shutdown -r +2`), so the run ends cleanly first.

A planned reboot or power cycle (a recovery owner cycling the host on purpose, for any project on it):
add `--reboot "<why>"` to the drain. It records the cycle in `state/host-cycle/<id>/record.json`
(`boot_id`, `started`, `reason`, `dry_run`, and the detached jobs it interrupts). On the next boot
every project here reads the records of all projects on this host: a detached job that a matching
cycle (not a dry run, started at most 2 h before the boot) cut short is not counted toward the
task's `budget.max_reboot_losses`, and the boot notice and the woken task's next run say it was planned.
A recovery tool of its own may write the same record itself.

The boot is told by the kernel's boot id (on macOS, kern.bootsessionuuid, else kern.boottime); where none can be read,
`--until-reboot` is refused.

## Template updates

- `ttp upgrade <name>` merges the installed tt-project template in a scratch worktree, checks the
  runtime compiles and imports, then fast-forwards this harness. It settles conflicts that need no
  judgment itself (both sides only added at one spot, or upstream already ships the change).
  Otherwise the harness is left as it was and a harness task is queued to finish the merge, at
  most one a day. It then exits 75 (deferred, not failed) while that task or the daemon's retry under
  `upgrade.auto` takes it on (the daemon retries only a newer version); when nothing does, it exits 1.
  The same conflict deferred past 48 h, or after two harness tasks that did not land it, is
  reported once as a normal observation.
  A finish task blocked or idle for 6 h no longer holds the harness against a host-side
  `ttp upgrade <name>`: that cancels and fences it (its `--apply` is then refused) and goes on.
- Conflicts: keep this project's intent, take upstream fixes. Resolve them in a scratch worktree,
  never in the live harness, and apply the result with `ttp upgrade <name> --apply <commit>`.
  Where the service manager is out of reach (a sandboxed worker), it asks the running daemon to
  restart itself (state/restart.request) and rolls nothing back while the old daemon still ticks.
  Exit 75: applied, restart deferred to the daemon; exit 1: the restart failed or was rolled back.
- One merge at a time: `ttp upgrade` holds the `harness-upgrade` lock for the whole merge, refuses
  while another upgrade runs, and stops if `main` changed the template's files meanwhile. An open
  upgrade task holds the live harness until it ends: a newer release moves `upstream` and retargets
  that task (exit 75) instead of queuing another. The harness repo refuses any other merge of
  `upstream` into `main`.
