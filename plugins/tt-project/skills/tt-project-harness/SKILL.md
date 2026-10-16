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

A row that links to a file under `references/` keeps its details there, one sentence per line:
read the one for the lever you change.

| Lever | File |
|---|---|
| Coordinator rules | `prompts/coordinator.md` |
| Worker contract, per-kind rules | `prompts/worker.md`, `prompts/kind-*.md` |
| Tiers, caps, debounce, idle wake, resources | `project.json` (defaults: `runtime/ttp/project.py`) |
| Task pin verification | `runner.pin_verify` (`lenient` by default, or `strict`): whether an explicitly model/effort-pinned task runs when its provider cannot verify the pin. See [references/task-pin-verification.md](references/task-pin-verification.md) |
| Recurring work | `schedules.json` when the harness has it (the daemon applies edits; `ttp schedules <name> --export --yes` moves them there once and commits; to only read one, `ttp schedules <name> show <schedule>`), else coordinator `schedule_set` or the web app's Recurring pane |
| Memory tidy-up | `ttp memory <name> --forget <entry> <entry> ... [--why W]` retires several entries in one call: one MEMORY.md rewrite and one harness commit, far faster than one call per entry. A name it cannot retire (unknown, ambiguous, standing without `--why`) is reported and the command exits non-zero; the others are still retired. To merge overlapping entries use `ttp memory <name> "<merged>" --kind K --supersedes <entry>...` (one commit; refused whole if it would drop a standing flag, change a user rule's kind or lose the user's quoted words); `--search <words>` finds live and archived entries. Decisions and facts no prompt has shown for `memory.archive_hidden_days` days (default 7) are archived daily, model-free |
| Watchers | `kind: command` schedules; scripts under `watchers/` in the harness. Output lines (`key`, `episode`, `whole`, `cleared:`), exit codes and errors, paging another project, Jev screening. See [references/watchers.md](references/watchers.md) |
| Self-healing checks | A `kind: command` schedule with a `heal` block, run by the daemon with no model: `check` or `preset`, `fix`, `resource`, escalation, outages, known faults and pauses; `ttp heal list\|test <check>`. See [references/self-healing.md](references/self-healing.md) |
| Box-clean gates | `tt-box-clean-probe` (in the harness's `bin/`): a model-free, read-only probe for "has this box been N hours without a device incident?". See [references/box-clean-gates.md](references/box-clean-gates.md) |
| Readiness probes | A probe that waits for a device or box checks it live, never a bare marker file; a broker hold's end with `ttp broker-released`; exit 255 is ssh's own only. See [references/readiness-probes.md](references/readiness-probes.md) |
| Time to recover | `ttp ttr`: model-free time to recover per machine, from incidents the daemon records. See [references/time-to-recover.md](references/time-to-recover.md) |
| Checks fast path | Opt-in, off by default: with no `delivery.checks_fast_globs`, every diff runs the full checks. A project that lists globs there gets it: a diff whose paths all match them (never code, tests, templates, skills: `delivery.checks_fast_exclude`) runs only the whitespace and CRLF checks; the pass is not a full pass and never counts for a PR or `on_pass`. `ttp checks --full` runs everything, also when queued or relaunched; a recorded full pass is reused. Known gap: the project owns its globs; one covering installed or test-asserted content (such as a plugin README that tests read) skips the checks those changes need. |
| Push check priority | `ttp checks` and push checks share a host-wide slot cap (`checks.host_max`), waited for in arrival order. Checks gating a push to the push branch (push-queue batches, `ttp push`) skip that queue: each push branch has one priority slot beyond the cap, host-wide, so approved changes never wait behind task checks. Task checks keep their cap and order; `ttp push --own` queues like them. The batch log and `ttp status` show `push priority slot: <holder>`; slots are OS file locks, so a killed holder frees its slot. |
| Push check outputs | `delivery.push_checks` receive a writable absolute `TTP_RUN_DIR`, never empty or `/`. Foreground `ttp checks` uses the worker's run directory; detached checks, detached pushes and push-queue batches get unique directories under project `state/check-runs/`, outside the checked worktree. Use `"$TTP_RUN_DIR/report.json"` for durable outputs. `checks.pid` and queue markers record `check_run_dir`; a restarted daemon rebuilds queued directories and a rebooted check invocation gets a fresh one. Outputs survive worker cleanup and completion; only detached checks' temporary scratch is reclaimed once its lifetime lock is free. Symlinked directories or locks are refused. Missing, invalid or unwritable output storage fails before checks; never skip a check or substitute `/`. Detached launches strip `TTP_PIDNS` and `TTP_CGROUP`. |
| Model-free wait endings | A waiting hand-off's `on_pass` or `on_pass_cmd` ends the task with no model run; `ttp task set-when`, `ttp ack-wait`, and where a detached `ttp checks` keeps its output. See [references/model-free-wait-endings.md](references/model-free-wait-endings.md) |
| SSH preflight | `ttp ssh-preflight <host>` or `@<resource>` (the host from config `device.hosts` or `device.runners.<name>.host`, looked up each run) runs `ssh <host> true` with `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ControlMaster=no` and `ControlPath=none`, so neither a lax `StrictHostKeyChecking` in the user's ssh config nor an open shared connection hides a box that is gone or presents another key. Exits: 0 reachable; 10 host key did not verify (changed, or not in known_hosts); 11 authentication failed; 255 not reachable (or 12 with `--classified`, for scripts that want a code of their own); 2 an unknown `@<resource>`. As a `retry_when` or `start_when`, 255 keeps the task asleep and counts towards the network-outage alert, while 10 and 11 wake it as a broken probe: waiting will not fix them. Use it instead of a local ssh wrapper. |
| Stock goal checks | `ttp goal suite_green\|artifact_fresh\|service_up`: model-free "is this goal met?" for a probe, `on_pass_cmd`, heal check or watcher. See [references/goal-checks.md](references/goal-checks.md) |
| Setting holds | While `delivery.allow_protected_push_branch` refuses the push branch, landing tasks wait on the open ask with their attempts untouched. See [references/setting-holds.md](references/setting-holds.md) |
| Charter grants | The daemon (runtime `grants.py`) compares the charter's push and auto-merge grants with the delivery settings, model-free. See [references/charter-grants.md](references/charter-grants.md) |
| Charter guard | The coordinator's `charter_update` (runtime `coordinator.py`, `_reject_contradicting_append`) refuses added text that a standing Restrictions item would contradict, quoting the item and naming the forms that pass: a quoted resend, `both_hold` plus a companion edit for an exception, or `both_hold` (or the text saying it loosens no restriction) when both hold. A companion edit keeps the quoted item word for word and only adds a clause naming, by id, an exception that stands in a temporary or dated Restrictions section, with no object or action of its own, on the sentence that exception is about; the user's yes to that exception covers it in a later turn and it is checked against its own item only. Removing or really loosening an item still needs the user's word or `over`. A refusal that names such a fix raises an immediate `charter_retry` event, at most two per change. Raise `CHARTER_GUARD_VERSION` and its pin when the guard changes. |
| Obligations | `ttp obligation` (runtime `obligations.py`): a deliverable owed to the user, tracked on its owner tasks until a receipt. Each owner has a 2 h durable-progress clock; a milestone (`key`, absolute `due`, `evidence_required`, set by the coordinator's `obligation` action) raises one high `obligation_overdue` episode at its due time without its receipt (`receipt <id> "<evidence>" --milestone <key>`), whatever the heartbeat or other progress. Known gap: nothing checks that an owner can advance it, so an obligation whose owners all wait or stall is caught only by the 2 h progress rule (or a milestone falling due); a daemon-side coverage check was dropped for its false alarms on ordinary waits and review stacks. |
| Shared watchers | A watcher script that workers also run (at start or hand-off) takes a non-blocking lock (`flock -n` on a file under the harness state, or `ttp lock` with a short wait) and exits quietly if busy, re-checks the condition before it acts, and dedupes by condition key. It also finishes in seconds in a big checkout, well inside a tool call's default timeout: `git status --porcelain --untracked-files=no --ignore-submodules=all` when untracked files do not matter, `--untracked-files=normal` when they do (one line per untracked folder, so a virtualenv, data or build folder is never walked file by file), never `--untracked-files=all`; each git or network step gets its own timeout (`timeout 20 git ...`, `GIT_TERMINAL_PROMPT=0` for fetch and ls-remote), and a step that times out prints `{"text": "<watcher>: skipped: <step> timed out after <N>s", "error": true}` and the rest still runs. |
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
  tt-project plugin's source repository or another project's harness. Upgrading another
  project on this machine to the installed release (`ttp upgrade <name>`) is not editing its harness; hand edits to its charter, memory, config, state or code are.
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
- Closing a note: the maintainers record `ttp upstream --resolve <fp> --fixed-in <version>` (or `--wontfix "<reason>"` for a note that is not a
  plugin bug: closed, no release), or a
  code task's hand-off lists the note in `resolves` and the push queue records it with the version
  its batch pushed. A sandboxed `--resolve`, `--ack` or `--regress` is queued in the run's directory and
  recorded when the run ends. Work
  waiting on it hands off `waiting` with retry_when `ttp upstream --fixed <fp>` (0 once this
  project's running daemon has the fix). The daemon then tells the coordinator once, and the
  project closes the note with `ttp upstream --ack <fp> "<evidence>"`. With no ack a day after the
  resolution, it shows as "shipped, not confirmed"; the coordinator gets one reminder 72 h after
  the fix reached this project, and with no ack and no regression the note closes by silence after
  14 days. If the fix did not hold, run `ttp upstream --regress <fp> "<reason>"` (filer project only,
  after a resolution; stays on this machine): it blocks the close by silence. Filing the same note
  again after its resolution counts as a regression. `ttp upstream --mine` lists this project's notes.

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
   (`ttp detach --remote`), where this host's reboot cannot end it. A job started with
   `ttp detach --resumable` (only one that is safe to rerun from the start) is started again by
   the daemon after the reboot while its task still waits on it, with no model run, up to
   `budget.max_job_relaunches` times (default 3). Abrupt reboots it relaunches through count toward
   `budget.max_reboot_losses`, so a job that takes the host down still gets its task blocked.
2. `ttp pause <name> --until-reboot --reason "host power cycle"`: the daemon lifts this pause on its first tick after the host
   booted again and says so in the feed. A plain `ttp pause` or `ttp resume` replaces it.
3. Write the hand-off (`waiting`, with a `retry_when` that holds once the host is back: the daemon
   probes it after the boot and wakes the task when it passes), and start the reboot a little later
   (for example `shutdown -r +2`), so the run ends cleanly first.

A planned reboot or power cycle (a recovery owner cycling the host on purpose, for any project on it):
add `--reboot "<why>"` to the drain. It records the cycle in `state/host-cycle/<id>/record.json`
(`boot_id`, `started`, `reason`, `dry_run`, and the detached jobs it interrupts). On the next boot
every project here reads the records of all projects on this host: a detached job that a matching
cycle (not a dry run, started at most 2 h before the boot) cut short is not counted toward the
task's `budget.max_reboot_losses`, and the boot notice and the woken task's next run say it was planned.

`--reboot` also holds the whole host, not only this project. As it starts, the drain writes a
power-cycle intent for this host into the shared machine ledger. While the intent is due, every
project's daemon on this host starts no new worker or reviewer run and no push-queue push, and
`ttp checks` exits 75 instead of starting; coordinators still run, and workers already running get
the usual notice to hand off. `ttp status` and the digest's paused section show the hold. The drain's
wait and its list also cover the other projects' runs and pushes on this host (their coordinators
are listed, not waited for); its exit codes do not change. The hold lifts when the intent closes
(a record for it with `dry_run` or `rebooted` false, or `ttp resume` before the drain recorded the
cycle), once the host booted again, or 10 min after the intent's time, with one low feed line in
each project. Other projects are held only when this host is in `ttp machines list`: the ledger is
keyed by its alias there.

A recovery tool of its own may write the same record itself, and must fsync it (and any other state
it writes) before the cycle: on some filesystems a record written seconds before a power cycle is
lost otherwise. Such a helper pauses the project with `ttp pause <name> --until-reboot --reason
"host power cycle"`, never a plain pause: a plain pause outlives the cycle and nothing lifts it.
`--until-reboot` without `--reason` records the reason "host power cycle (until reboot)".

`--reboot` also writes a power-cycle intent for the host in `ttp machines`, or takes up one already
due there, and every project on the host holds new runs until it. The hold lifts when the intent
closes (the drain that wrote it records no reboot, or `ttp resume` after it recorded nothing), when
the host boots again, or a while after the intent's time. A drain that only took up another
project's intent never closes it, since that cycle is still coming: once it and every later drain that
moved the intent record no reboot, the intent gets back its own time and window.

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
