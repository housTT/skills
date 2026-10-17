# tt-project

Long-running, self-driving projects that run on your own machines.

Tell your agent "start a tt-project called `docs-refresh` that keeps our README accurate", and a
project comes up with its own coordinator. The coordinator plans the work and hands it to
headless workers in isolated workspaces. It watches pull requests and logs, fixes what it finds,
remembers what you tell it, and stays inside a budget. Close the chat and it keeps going. Open any
chat later, in Claude Code, Codex or Cursor, and connect to it by name.

Nothing runs in a hosted cloud. The project runs on this machine or on an always-on box you
name, which is also where it can use local hardware.

## Install

```text
/plugin install tt-project@tenstorrent-skills      # Claude Code, after adding the marketplace
codex plugin add tt-project@tenstorrent-skills     # Codex
```

Cursor: copy the plugin folder into `~/.cursor/plugins/local/tt-project` (Cursor skips a symlink
there that points outside that folder).

Until tt-project is on `main`, install it with the one-line install below, or as described in
[Dogfood install](#dogfood-install).

### One-line install

Paste this line into a terminal. It installs tt-project for each of Claude Code, Codex and Cursor
that it finds and skips the others with a note. Set `TT_PROJECT_REF` at its front to a release tag
of `tenstorrent/skills` (a branch or commit also works); left empty, it installs the default branch.

```bash
(TT_PROJECT_REF=; d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT && git init -q "$d" && git -C "$d" fetch -q --depth 1 https://github.com/tenstorrent/skills.git "${TT_PROJECT_REF:-HEAD}" && git -C "$d" -c advice.detachedHead=false checkout -q FETCH_HEAD && if [ -f "$d/plugins/tt-project/install.sh" ]; then sh "$d/plugins/tt-project/install.sh"; else echo "tt-project: no installer at ${TT_PROJECT_REF:-the default branch}; set TT_PROJECT_REF to a release tag" >&2; exit 1; fi)
```

It fetches that one commit with `git` and your own git credentials and runs
`plugins/tt-project/install.sh` from it. The installer keeps one clone at `~/.local/share/tt-project/skills` (under `$XDG_DATA_HOME`
when set), adds it as the `tenstorrent-skills` marketplace for Claude Code and Codex, copies the
plugin into Cursor's local plugin folder and ends with `ttp setup`. Rerun the line to update, or with
another tag to switch: it updates that clone and the same entries in place. A ref without the
installer fails with a message to set `TT_PROJECT_REF` to a release tag. If one tool's step fails
(say Codex), the installer still installs for the others and runs `ttp setup`, then lists the failed
steps and exits non-zero.

Rerunning the line with the same `TT_PROJECT_REF` installs whatever that ref points at now: a branch
gives its latest commit, and a release tag the publisher moves forward as a release channel gives the
latest release. A tag that is never moved keeps you on exactly that release.

When it ends, the installer says how to load the plugin, only for the tools it installed for:

- Claude Code: start a new session, or in an open one run `/reload-plugins` (and `/reload-skills`
  for the plain `/tt-project`).
- Codex: start a new session; open sessions do not see it.
- Cursor: restart it, or run **Developer: Reload Window**. On Teams and Enterprise plans an admin
  must allow local plugin imports.
- `ttp`: works in an open terminal when `~/.local/bin` is already on its `PATH`; otherwise add that
  folder to `PATH` in your shell profile and open a new shell.

The first use installs a `ttp` command for your user (`ttp setup`). Python 3.9+ and `git` are the
only requirements; the runtime uses the standard library.

To call the skill directly instead of just asking for a project:

| Host | Main skill | Harness skill | Device box skill |
|---|---|---|---|
| Claude Code | `/tt-project` | `/tt-project:tt-project-harness` | `/tt-project:setup-box` |
| Codex | `$tt-project:tt-project` | `$tt-project:tt-project-harness` | `$tt-project:setup-box` |
| Cursor | `/tt-project` | `/tt-project-harness` | `/setup-box` |

The device box skill gets a box (bare metal, a reservation or a Slurm job) and has agents edit,
build and run in their own checkout on it over ssh. Site values stay in a local
`~/.tt-project/boxes.json`, never in a repository.

Claude Code always shows a plugin's skills under the plugin's name, so its own entry for the main
skill is `/tt-project:tt-project`. `ttp setup` adds a small personal skill,
`~/.claude/skills/tt-project/SKILL.md` (under `$CLAUDE_CONFIG_DIR` when set), that the slash menu
lists as `/tt-project` and that loads the plugin's skill. Only you can run it, so Claude still sees one
tt-project skill, not two. Every `ttp setup` refreshes it. If you already have your own skill named
`tt-project`, setup leaves it as it is. `ttp setup --no-shortcut` removes the shortcut.

## Dogfood install

tt-project is not on `main` yet, so adding the marketplace from `main` does not find it. Install
a release ref instead: a tag or branch of `tenstorrent/skills` that your team publishes for tt-project.
Set `TT_PROJECT_REF` to it (the commands below use `$TT_PROJECT_REF`; the one-line install above reads the same
variable, set at its front, and empty means the default branch). You need Python 3.9+, `git` with `user.name` and `user.email`
set, and a logged-in agent CLI to run the project's workers.

Claude Code (a branch or tag goes after `#`):

```bash
export TT_PROJECT_REF=<release tag or branch>
claude plugin marketplace add "https://github.com/tenstorrent/skills.git#$TT_PROJECT_REF"
claude plugin install tt-project@tenstorrent-skills
claude plugin details tt-project    # Skills (3): setup-box, tt-project, tt-project-harness
"$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Codex (`--ref` takes a branch or tag):

```bash
codex plugin marketplace add tenstorrent/skills --ref $TT_PROJECT_REF
codex plugin add tt-project@tenstorrent-skills
"$(ls -d "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/tenstorrent-skills/tt-project/*/ | sort -V | tail -1)bin/ttp" setup
```

Cursor installs plugins from a team marketplace or a local folder, and neither takes a git ref, so
check out the tag with `git` and copy the plugin into Cursor's local plugin folder. These steps
follow Cursor's plugin documentation and have not been tested end to end yet:

```bash
git clone --depth 1 --branch $TT_PROJECT_REF https://github.com/tenstorrent/skills.git tt-skills-dogfood
mkdir -p ~/.cursor/plugins/local
cp -R tt-skills-dogfood/plugins/tt-project ~/.cursor/plugins/local/tt-project
~/.cursor/plugins/local/tt-project/bin/ttp setup
```

Then restart Cursor (or run **Developer: Reload Window**).

`ttp setup` installs `ttp` in `~/.local/bin` (add that folder to `PATH` if it is not there yet)
and adds the plain `/tt-project` command for Claude Code. The skill also runs it on first use.

First project: in a chat, say "start a tt-project called `demo` in this repository: <brief>", or
from a shell inside a git repository:

```bash
ttp new demo --describe "Keep the README accurate."
```

It ends with a checked link, `web app: http://127.0.0.1:<port>/#token=<token>`. Open it in a
browser; `ttp web demo` prints it again. `--provider claude|codex|cursor` picks the agent CLI that
runs the workers (default: the chat's own CLI; Claude Code from a plain shell). The coordinator starts on the brief right away.

Update: remove the marketplace, then repeat the install commands above with the new tag or branch,
`ttp setup` included. Running projects take in the new release within an hour and keep their work.

```bash
claude plugin marketplace remove tenstorrent-skills    # also uninstalls its plugins
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
```

When you follow the branch, Claude Code can also update in place (then run `ttp setup` as above):

```bash
claude plugin marketplace update tenstorrent-skills && claude plugin update tt-project@tenstorrent-skills
```

Uninstall: stop every project (`ttp list` names them), then remove the shortcut, the plugin and
the per-user files (project registry, saved keys, ttp's cache and the installed `ttp`):

```bash
ttp stop <name> --kill          # each project: ends its daemon and workers, removes its service
ttp setup --no-shortcut
claude plugin marketplace remove tenstorrent-skills
codex plugin remove tt-project@tenstorrent-skills && codex plugin marketplace remove tenstorrent-skills
rm -rf ~/.cursor/plugins/local/tt-project
rm -rf ~/.tt-project ~/.local/bin/ttp "${XDG_CACHE_HOME:-$HOME/.cache}/ttp"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/tt-project"    # the one-line install's clone
```

Each project keeps its files in `<project root>/tt-project/`; delete that folder too to drop its
history. If you ran `ttp notifier install`, also remove its `tt-project-notifier` user service.

### Known issues

- Cursor install is verified by clone and `ttp setup` only, not end to end.
- The setup-box IRD route is tested against a fake `ird`; a live run needs a network that reaches the IRD service.
- Codex and Cursor spend is estimated from a price table (see Limits).
- Not on Codex: worker plugins. Not yet on Cursor: worker plugins, worker isolation, mid-run coordinator updates (see Limits).
- Older Cursor builds print nothing until a run ends, so set `stall_s` above their longest run.
- A laptop pauses while it sleeps; use an always-on machine for round-the-clock work.

## Using it

| You say | What happens |
|---|---|
| "Start a tt-project called X on box B: <brief>" | the project is created on B and starts working |
| "Connect to project X" | this chat attaches; replies and alerts arrive here |
| anything addressed to the project | relayed to the coordinator verbatim (the chat's own context goes apart, in `ttp say --context`, and never counts as your word); its answer comes back to this chat |
| "What is X doing?" | `ttp status X`: running work, blockers, spend, why idle |
| "How are all my projects?" | `ttp overview` (or the web app's Projects tab): one line per project on this machine |

The brief can be inline text, a file, or links. Goals, restrictions ("never access the internet")
and preferences you add later become part of the project's charter and memory.

## How it works

```
 chats (any host) ─┐                ┌─ worker: headless agent in its own worktree ─┐
 web app ──────────┼─ inbox ─► daemon ─► coordinator turn (decides, never works)   ├─► results
 watchers ─────────┘   (deterministic)  └─ worker … (parallel, budgeted, stall-guarded)┘
```

- **Daemon** (one per project): schedules, watchers, budget gates, message routing, web app.
  Deterministic and cheap. It starts a model only through a run.
- **Coordinator**: a short, tool-less decision over a digest of the project, batched and rate
  capped. Anything needing files, commands or deep thought becomes a task.
- **Workers**: one task each, in a git worktree on their own branch for code, with a dollar
  budget, a wall clock, and a stall guard. Each hands off a structured result. A worker whose
  machine or queue is busy hands the task back to retry later, instead of waiting inside the run.
  The wall clock and stall guard count only time the host is awake. A run a host sleep cut short
  is retried without spending an attempt and is not counted as waste. After a wake, nothing new
  starts until the host has been awake for `budget.wake_settle_s` (default 300 s), so a laptop's
  brief maintenance wakes start nothing. Messages from people are still answered at once.
  A run a reboot, a sleep or a lost supervisor cut short after real progress
  (`budget.resume_lost`, default $0.50 or 10 min) continues its agent session in the same
  working directory with a short prompt, instead of starting over.
  A task lost to `budget.max_reboot_losses` abrupt reboots is blocked, as it may cause them. So is,
  at once, a task whose run or detached job held a `ttp lock` resource of this host (a device, a
  machine; not a push lock) when the host went down abruptly: the coordinator requeues it once the
  attempt is known not to be implicated. A
  detached job cut by a planned host cycle (`ttp drain <name> --reboot "<why>"`, or another project's
  `state/host-cycle/<id>/record.json` on the same host, or a `ttp machines change add <this host>
  --power-cycle-at <t>` intent) does not count. Such a reboot is reported once as planned, at low
  severity, naming what planned it; only abrupt, unplanned reboots count toward the "host looks
  unstable" warning.
  When the coordinator rescopes a running task, the change reaches the worker mid-run.
- **Memory and charter**: plain files in the project's harness, one fact per file. A temporary entry carries its end (a time, a plain-language condition or a probe); the daemon retires it to the archive or `CHARTER.history.md` once that passes and says so.
- **Watchers**: pull requests (CI, reviews, mergeability) and logs, reporting only changes.
  With Jev enabled (see [Jev](#jev-optional-add-on)), new observations are screened by a cheap
  decision model first.

Keyed alerts keep their history when closed. `ttp alerts <project>` shows their message IDs;
`ttp alerts <project> --ack <id> --why "<reason>"` acknowledges that message's episode with the
reason. One whose condition is over closes; one that still holds moves to the feed and stays quiet
until it clears, so a new occurrence alerts again. A live outage (logout, unreachable hosts, full
disk, machine or whole-box heal outage) cannot be acknowledged away. Retrying preserves the
original receipt, and an unknown ID is refused. A heal check's
user alert also clears when escalation moves to the coordinator or a known fault covers its
current failure; the check remains unhealthy until it passes. Known faults never silence a
prolonged whole-box outage.

## Budget

- Subscription plans: a plan's capacity is lost at each reset, so the project uses it. Below the
  line (90%, `100 - budget.reserve_pct`) it runs all its parallel workers; it does not spread use
  evenly over a window. Running work keeps using the plan after it starts, so near the line it
  estimates what each worker will still add (the account's measured burn per worker, times how
  long a run usually lasts here) and runs only as many workers as fit under the line: fewer in the
  last stretch, none new while those are all busy or the running ones alone would reach it. At the
  line nothing new starts until the window resets; the rest stays yours. Running work is never
  stopped.
- The 90% line is the default (`budget.reserve_pct` 10). If you share the plan with your own
  interactive chats, the project may use all but 10% of each window, so only that 10% is left for
  you. To keep more, raise the reserve: `ttp config <name> budget.reserve_pct 30` keeps 30%.
- Free Codex window resets (an account's `rateLimitResetCredits`) are used automatically, at most one
  per window, earliest-expiring first: when a window holds work at the line and resets more than
  `budget.reset_credit_min_hours` (12) from now, or when a reset expires within
  `budget.reset_credit_expiry_hours` (48) and a window is at least `budget.reset_credit_expiry_use_pct`
  (50) % used. Projects on one machine sharing an account never use two for one window.
  `ttp config <name> budget.reset_credits_auto false` turns it off.
- In that last stretch deep tasks run at the standard tier. `ttp status` and the web app say when
  the line holds new starts back. You get an alert only when the project reaches the line, once
  per window; it clears by itself when the window resets.
- Usage-billed accounts: $100 per 24 hours and $200 per 7 days per project by default
  (`budget.daily_usd`, `budget.weekly_usd`). A new run
  starts only if its budget fits in what is left of both caps. A plan account whose successful
  runs stop reporting plan windows falls under these caps too (failed or silent runs do not count).
  The caps count only spend made on an account that was billed by use at the time: after a switch
  from a plan account to a usage-billed one, the plan's earlier spend stays out.
- Global daily cap (usage-billed, $200/day by default): `budget.global_daily_usd` stops new work in
  every usage-billed project on the account once the whole account's tt-project spend today reaches
  it. "Today" is the rolling 24 hours unless `budget.day_start` is set. Change it for every project
  on the machine with `ttp config --account budget.global_daily_usd 500` (or for one project with
  `ttp config <name> budget.global_daily_usd 500`); 0 turns it off. Plan accounts are bounded by their
  windows instead and never meet it. Running work and replies to your messages go on; it clears by itself at the day's reset
  (or, without a budget day, as spend leaves the last 24 h). Only spend on the same provider and the same account counts (an account is compared by a
  hash, so its name never leaves the machine); rows with no account recorded count, to be safe.
  The total covers every project `ttp list` shows on this machine, the machines of the registry's
  projects elsewhere, and machines tagged `tt-project` in `ttp machines` (`ttp machines add <alias>
  --tags tt-project`). Other machines are asked over ssh (BatchMode, no prompts) with
  `ttp spend-today`, at most every 10 minutes; their answers are cached in
  `~/.tt-project/global-spend.json`. A machine not heard from for 30 minutes still counts with its
  last answer for the same day and is shown as stale. A machine that can reach another but not the
  other way round (a laptop that can reach a server, but not the server the laptop) pushes its own
  `spend-today` there instead: with the global cap on, each machine sends it over ssh to
  `ttp spend-today --receive` on every machine it asks, every 10 minutes (every 6 hours to a machine
  that says it already asks this one; less often after failures), in the background. Pushed answers
  are kept in `~/.tt-project/global-spend-pushed.json` and count like asked ones, stale after 30
  minutes the same way; a machine both asked and pushing counts once, with its newer answer.
  Each answer, asked or pushed, also carries that machine's estimate of its other Claude Code
  sessions (`other_sessions`: provider, account hash, dollars, `estimated: true`; `null` while its
  logs are not read yet). It counts with that machine's answer, so once per machine, and the total
  names it apart from this machine's own sessions. An answer from an older tt-project has no such
  field: it still counts, its sessions count 0, and the total says they are missing.
  `budget.push_spend_to` (account-level) names the machines to push to instead, or `"none"` turns
  pushing off. If the total cannot be worked out at all, the
  per-project caps stay in charge. Web, desktop and cloud sessions are not seen; another
  source can be added in code with `globalcap.add_other_source(fn)`, where
  `fn(provider, account, start, end)` returns `(usd, label)`.
- Budget day: with `budget.day_start` set (`"HH:MM"`), "today" is a fixed day starting at that time
  in `budget.timezone` (an IANA name such as `Europe/Berlin`; unset, the project's home zone, below;
  the host's own zone plays no part), 23 or 25 hours long across a daylight-saving change. The daily cap and the global
  cap count that day. Empty (the default) keeps the rolling 24 hours. Weekly caps stay rolling.
- Home time zone: each project keeps the IANA zone of the user's workstation as `home_timezone` in
  its `project.json`. `ttp new` records the zone of the machine it runs on (with `--host`, the
  workstation's, never the box's). `ttp connect` from a workstation (not from an ssh login), and a workstation's spend push
  for the projects it set last, send its current zone; a change is logged once in the feed, so the
  zone follows a travelling user. A project without one gets the account's `budget.timezone`, else the
  machine's zone, once at daemon start. Times shown to the user use it (`runtime/ttp/timefmt.py`).
- These keys (and `budget.push_spend_to`) are account-level: `ttp config --account KEY VALUE` writes them to
  `~/.tt-project/settings.json` (mode 0600), which every project on the machine reads under its own
  `project.json` (a project may still override a key, except `budget.push_spend_to`, which is read
  from the account settings only). `ttp config --account KEY` reads one; an
  empty value removes it. `ttp spend-today [--json]` prints this machine's tt-project spend for the
  budget day by provider and account hash, and its other Claude Code sessions as estimated; it is
  what other machines ask for.
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
- A coordinator turn that goes over `coordinator.turn_budget_usd` is never retried as is: its batch is
  halved down to single items; a single item runs once more alone at the ceiling
  (`coordinator.turn_budget_ceiling_usd`, 0: twice the cap, never above the hourly coordinator limit
  or the dollar caps). One that fails even then is parked and you get one alert naming every parked
  item; it runs again once you raise the cap or the ceiling. The other items go on.
- A review runs light when the diff it checks touches no `review.risky_paths` glob and no code file
  with a risky name (`review.risky_names`: state, database, schema, migration, push, budget, billing,
  spend, spend cap, release and upgrade files by default; `[]` turns it off), and is docs and tests
  only or small (`review.light_max_lines` non-doc lines, default 80); standard otherwise. Every re-review after a
  failed review and every retry runs standard; only the coordinator picks deep. An optional
  `review.light_paths` glob list narrows light further: every non-doc file must match one of its
  globs (unset or empty: any path may go light). The review run's note records the pick and the
  rule behind it (`review_tier`).
- When delivery has a review step (`delivery.review_before_pr` or a `delivery.push_branch`), the
  daemon queues the review of each finished code task whose hand-off has no follow-ups, notes or
  findings itself (`review.auto`, on by default), and that hand-off starts no coordinator turn.
  Any other hand-off leaves the review to the coordinator's turn. A code task still in review
  with no review `review.sweep_after_s` (default 1800, 0 off) after its last run gets its review
  from the daemon. The sweep only queues reviews and never closes a task, so a task whose
  continuation already passed review still gets its own review (one review each, no work lost).
  The exception is a task whose change already landed on the push branch under other commits
  (the same changed lines, its `delivery.version_bump` files and changesets aside, commit by
  commit or squashed): the sweep, the daemon's review and the fix of a failed review record it as
  superseded (as `ttp task superseded` does), close it if it is in review, tell the coordinator
  once, and queue no review or fix for it.
  `review.auto_notes` is added
  to each such review's spec (for example, what to do after a push). When the task's PR already
  carries the reviewed head (as pr-watch last read it, or as the task's own `ttp push --own`
  pushed it), the work is delivered: the review is review only, with no push to
  `delivery.push_branch` and no push-queue approval. So is the review of a change that must not
  reach the push branch: every file its diff since the base changes matches
  `delivery.push_exclude_paths`, its hand-off sets `no_push` (true or the reason), or its spec has
  a line starting `no_push:` (the reason follows). A continuation (a fix of a review's
  findings, a `continue:` task) inherits the nearest ancestor's `no_push` unless it says
  `no_push: false` (or its hand-off sets `no_push` false). Prose is never read as a ban: free text cannot
  tell a ban from a sentence that reports, quotes or conditions one. The review's spec says why, its fix and re-review
  stay review only, and the push queue ignores its approval. Every review is review only while
  `delivery.push_branch` can never be pushed to: it names main or master, which `ttp push` always
  refuses, or the code repo has no git remote (or not the one it names). The config check and
  `ttp doctor` say so. A review the coordinator adds
  for the same work replaces the daemon's while it has not started. A review that fails with
  follow-ups gets one fix task on the reviewed branch and a re-review from the daemon, and the
  tasks waiting on the failed review wait on the re-review instead of being blocked (at most two
  rounds per stack; a failure without fix follow-ups still blocks them, and `upstream:` notes and
  deferred follow-ups stay with the coordinator). Across stacks, once `review.area_fail_cap` (3; 0
  off) reviews of one area failed within 48 h (a lineage linked by `continues`, follow-ups,
  `depends_on` and `Review #N` / `Fix review #N` titles, or overlapping changes to the same main file), the
  daemon queues no more fixes there and raises a high-effort coordinator turn to re-plan it
  ("repeated review failures in one area", also counted in the daily review).
- A review's result: `done` lets the change proceed (its PR, push or push-queue approval and its
  dependents), `failed` stops it and starts the fix flow above. A review handed off `done` with
  `metrics.verdict` `changes_needed` (or `changes_requested`, `rejected`) counts as failed too, so a
  project may word the rule that way. The rule sits in its own block of `prompts/kind-review.md`,
  between `<!-- ttp:result-rule -->` marker lines the prompt leaves out: a project that rewords it
  keeps its wording through template upgrades without merge conflicts.
- Each tier maps to a model and effort per provider (`providers.<name>.tiers`). The coordinator
  runs at `coordinator.tier` (light) unless `coordinator.model` or `coordinator.effort` is set:
  those pin the coordinator alone. Unset, it takes its tier's effort and the model of
  `coordinator.model_tier` (standard by default; "" uses its own tier's), so the cheaper light-tier
  model does not move the coordinator. The model names one of `core_provider`'s models.
  `coordinator.turn_timeouts_s.<provider>.<effort>` sizes the turn after effort is resolved:
  low/medium/high/xhigh/max (or ultra) default to 300/600/900/1200/1500 seconds for Claude,
  and 300/600/1200/1800/2400 for Codex and Cursor; an unknown effort or provider gets 600.
  Above 32 KB of prompt text, each additional 8 KB adds 60 seconds. These allowances give slower
  reasoning turns more time while routine low-effort turns stay short. The base is the first one
  set: the project's own `turn_timeouts_s` entry for the provider and effort, then a flat
  `coordinator.turn_timeout_s` (0, the default, is unset; a stored 600, the old flat default, also
  counts as unset so older projects get effort sizing), then the built-in table. The prompt
  allowance is added to any base. `coordinator.turn_timeout_ceiling_s` defaults to 3600 and can be
  lowered; it caps every result, and the runtime always enforces a one-hour absolute ceiling. Each run records prompt bytes,
  provider, model, effort and chosen timeout; timeouts also leave a daemon log and diagnostic state.
  A tricky or blocking turn runs at least at `coordinator.unblock_effort` (high; empty turns this
  off), so it looks harder for a non-disruptive way forward. Its triggers, kept in one table
  (`coordinator.effort_triggers`) and recorded per turn in the run's note: a user message (and a
  change of plan in it); a blocked, failed or needs-review task, a failed review, a dead dependency,
  an expired or broken deferral or a stalled review; a task failing `coordinator.repeat_fails_24h`
  (2) times in 24 h; a task's external waits (not its own checks, jobs, push or planned window)
  changing reason, probe (`retry_when`) or owner (the task or ask they name), going on for 24 h, or
  reaching `coordinator.repeat_waits_24h` (8) in 24 h (the last two once per run of waits), where
  a wait on a live `ttp detach` job or an unpaused `ttp lock` resource counts only past
  `coordinator.live_waits_max` (6) waits on the same ones in 24 h or once the job's log stops growing; a
  rejected action; free worker slots while every queued task is held by a dependency or a paused
  resource or deferred on purpose (`start_after`, `start_when`), at least one of them held (planned
  deferrals alone are routine); a high or critical event or alert; resource trouble (not waits only); a costly or irreversible step (an ask timing out, a task's budget spent, PR findings
  or a clean PR, a failed push, the budget gate entering or leaving red); an idle wake finding
  blocked tasks or open asks. Lowering rules keep known or routine events at the base effort, each
  logged in the turn's note (`lowered`): a needs-review hand-off with no follow-ups, findings or notes
  whose review the daemon queues itself; a high or critical event whose fingerprint is a known case
  (an open issue an earlier turn already saw at this severity or higher, a deploy pending on an owned
  merge before its takeover, an open change in the shared machine ledger that expects it: `ttp machines
  change add ... --expect <fingerprint|down|held>`); and a review's failed hand-off counts once, as
  "failed review". Routine bookkeeping stays at the base effort; such a turn that finds
  its batch harder than it looked returns `escalate`, and the same batch reruns once at high
  effort (never twice, and never from a raised turn; kv `escalations` counts them). A pinned
  `coordinator.effort` wins over all of this. `coordinator.effort_skip_triggers` (none by default)
  lists trigger labels, as recorded in the note, that never raise a turn. With a Jev key, Jev also rates a turn the triggers
  leave below high 'routine' or 'needs thought', but only when it has a new failed or blocked task,
  high or critical event, user message, or an external wait older than `coordinator.jev_wait_h` (6);
  other turns skip the call and say so in their note. A reason rated at `coordinator.jev_threshold`
  (0.7) or above raises the turn the same way, and the same events never raise a second turn. Each
  call is logged with the effort it led to and what the turn did, and reported in the daily review;
  `jev.uses.coord_effort` = `off` turns the check off.
- Blocker sweep: every `coordinator.blocker_sweep_h` (6; 0 turns it off) hours while anything is
  stuck, and sooner (never within `coordinator.blocker_sweep_min_gap_h`, 1) when a pause ends, an
  ask is resolved, a dependency finishes, a provider pause ends or a re-check comes due, a
  high-effort turn lists every blocked, waiting, in-review, setting-held and deferred task and every
  paused resource with its age, reason and last re-check. The coordinator records one `sweep`
  outcome for each; items without one, `still_blocked` ones with no re-checkable condition, and
  re-block loops (the same reason twice, or a spec addendum while still blocked) are flagged in the
  next sweep. Reviews older than `coordinator.review_backlog_h` (6) get a line on why their review
  has not run. A hold no turn can lift needs no outcome and is listed as no action: a user pause (a
  probe that only tests for a marker file named `user-*`; `coordinator.user_pause_marker` sets the
  pattern), a resource pause the user set, or a deferral only until a time. A `human_indispensable`
  outcome on a task a user-set resource pause holds covers every task naming that resource until
  the pause row changes, a user message arrives, or another project's note relays the user's words
  (a non-empty `user's words (verbatim)` part); then the sweep judges them again. An outcome stored
  in an older format is no cover. A waiting task on a user-pause probe is never reported
  as a stale wait. The daily review lists everything stuck over 6 h with its sweep outcome.
- Promises: a coordinator reply or notify that commits to a deliverable or a time ("I'll report the
  sha by 15:00", "ETA 18:30") is always recorded as a promise. It holds the chat, the promised
  words, the due time (the time it names, in the home zone, else `coordinator.promise_default_h`, 6,
  later; a named date already past or over 14 days ahead is ignored and its clock time applies), the batch's task
  that replies to that chat, and any obligation the same batch records or attaches that such a task
  owns. The digest lists open promises and marks overdue ones; the blocker sweep lists an overdue
  one as an item. Once overdue, one high-effort wake asks for the deliverable or an honest update in
  that chat. A promise closes when its tasks finish and a reply reaches its chat, when every linked
  obligation has its receipt or is dropped, or when the
  coordinator closes it (`obligation` with its key, or a sweep outcome `closed` or `retired`).
  A reply whose `delivers` names an obligation or promise (`o1`) or its owner task (`#12`) marks it
  delivered; it closes once every owner task finished. A promise is delivered only in its own chat.
  Large sweeps split into durable consecutive batches, one per turn, with disjoint item coverage.
  `coordinator.sweep_prompt_chars` bounds the sweep section (12000 characters by default, clamped
  to 2048–16000); reasons and diagnoses are clipped to 200 characters each. This bounds added sweep
  context while preserving every identity and outcome request, including items beyond the old
  60-item cutoff. A restart or failed turn retries its batch; remaining batches stay queued.
- Ask rethink: an open `restriction`, `irreversible` or `human` ask that holds a blocked task
  longer than `coordinator.ask_rethink_h` (2; 0 turns it off) raises one `ask_rethink` event, a
  high-effort turn that decides the ask itself unless only the user can (then `human_indispensable`
  with a why). Each ask is raised once, across restarts; review, merge, access, funds and spend asks
  never are. A `still_blocked` on a task such an ask holds is flagged, not carried.
- Hold loops (model-free, every tick): work nothing will move raises one event while it lasts and
  clears once resolved. `time_only_hold`: held only until a time (`until:` or `start_after`) with
  no open dependency, ask, resource or probe, once its review passed, its time was re-armed or it
  stayed so past `coordinator.hold_max_h`; re-arming the time does not clear it.
  `blocked_dependency`: a queued or blocked task depends on a blocked task with no end of its own.
  `dead_dependency`: a blocked task depends on a failed, cancelled or missing one (both after a
  coordinator turn, which a re-hold no longer postpones). `dependency_cycle`: tasks that depend on
  each other, named; `task_add` and `task_update` refuse to create one. All are listed in the
  `stale_holds` event and flagged as loops in each blocker sweep.
- Provider failover (model-free): a queued, deferred or waiting task whose provider cannot start
  new work (logged out, a limit pause, or a red gate such as a plan window at the line) for longer
  than `coordinator.provider_failover_after_h` (4; 0 turns it off) moves to the first usable
  provider in `coordinator.provider_failover_to` (empty) or else the core provider. A pin to a
  provider other than the core one is a preference unless the task's `pin_required` is set (only
  when the user's own words require that provider; label `pin:required`, kept by its reviews, fixes
  and follow-ups): a preference moves at once, to the core provider first, whenever its provider's
  gate holds it. Its model pin is cleared, its effort and budget kept (the nearest effort the new
  provider takes), its tier set from the effort (e.g. Codex `xhigh` to `deep`), and a spec note says
  what moved, why and when the old provider resets. A required pin stays; running tasks are only
  reported. Each move is a feed event, one low alert and a line in the daily review.
- Stall check (model-free): when no worker run has started for `coordinator.stall_wake_h` (3; 0
  turns it off) while a queued task has been one dispatch would start now for that long, one high
  observation lists those tasks and when the last worker run started. It uses dispatch's own checks,
  so tasks held by a dependency, a timer or probe, a paused resource, a busy lock, a logged-out or
  offline provider, or the budget gate's slots are never counted: each hold has its own alert.
  Known gap: a hold that is itself wrong (a lock that never clears, a probe that can never pass) is
  not reported by this check; the lock, hold and wait checks cover it.
- Idle capacity (model-free): a device resource of the project (its device lock, a `*-device` name its
  config or recent tasks use, or a machine tagged `device` that its charter's Resources names) that
  no open task, `ttp lock` slot, reservation or detached-job hold uses for `coordinator.idle_capacity_s`
  (1500; 0 turns it off), while a worker slot is free, the gate is green or yellow and dispatch would
  start nothing, raises one `idle_capacity` event naming it, the running work and the next scheduled
  check. Paused resources, resources in trouble, machines with an open ledger condition or an open
  change entry that holds their device work (a held, paused or drained broker or queue, said as a
  phrase; file names and recovery switches such as auto reset do not count), and devices
  no open task names and no task run in the last `coordinator.idle_capacity_use_days` (7; 0: no
  filter) named never count. One event per idle stretch; one still idle is raised again only after
  `coordinator.idle_capacity_rearm_s` (6 h), doubling each time up to 48 h. When the coordinator
  handles an event without a task naming the device, that decline is kept per device in the
  database: its next idle stretch waits both the grace and the doubling rearm from its last event,
  until a task names the device again.
- Network outage (model-free): when every task that could start waits on a probe saying this
  machine cannot reach its host (ssh exit 255, confirmed by `ssh <alias> true` failing too; a 255
  that names no host, or whose host answers, is a broken probe for its task), no worker runs and no other task is ready to start, for
  `waiting.unreachable_alert_s` (900; 0 turns it off), one normal alert asks the user to check the
  network connection of the machine running the project, and its VPN or network sign-in if the hosts
  need one. It names the hosts, when the outage began (home time zone), how many tasks are held and
  that the project keeps running and starts other work once it is ready, stays in the top section, is sent once per outage
  (also across projects on the same machine) and wakes no coordinator. A heal check still trying its
  fix delays it by one more grace. It clears with one low feed line once no probe has said
  unreachable for `waiting.unreachable_debounce_s` (600), so a brief reconnect stays one outage.
  Hosts are named by the ssh or machines-list alias the probe used, never by the resolved address.
  When every host's route (`ssh -G <alias>`) ends at loopback, directly or through its ProxyJump or
  ProxyCommand, it is a tunnel from the user's workstation: the alert says the tunnel is down and
  reconnects by itself (no VPN advice) and is a low feed line, raised once to normal under the same
  key when the outage is `waiting.tunnel_outage_page_after_s` (3600; 0 pages at once) old.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.
- A task's row in the web app shows its chain's metric trend (the task, what continues it, its fixes
  and re-reviews): each numeric hand-off `metrics` value (`12.5` or `"12.5 ms"`) seen in at least two
  runs, with a sparkline and run, time in the project's home zone, value and delta per run.
- The web app's header and `ttp status` show the budget in one line, for example
  `5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%`.
  The windows and averages are the account's (an average is the mean of each completed window's
  peak: 5-hour windows over 7 days, weekly ones over 3 weeks). The dollars are this project's last
  24 h: `virtual` (list-price equivalent) on a plan, `actual` when billed by use (only spend made
  on a usage-billed account). A usage-billed account with a budget day or a global cap shows
  `today $1.20 this project, $85 of $1000 global - resets in 6.5 h` instead, with
  `(1 machine stale)` when a machine's answer is old.

## Model tiers

Each task runs at a tier: light, standard or deep. The default model and effort per provider
(`providers.<name>.tiers.<tier>`):

| Provider | light | standard | deep |
|---|---|---|---|
| Claude | `sonnet`, low effort | `opus`, high | `opus`, max |
| Codex | the account's default model, low effort | default, high | default, xhigh |
| Cursor | `auto` | `auto` | `auto` |

Light runs on a cheaper model where the provider's CLI names one without a version. Codex names
its cheaper models only by version, so light keeps the default model at low effort; set
`providers.codex.tiers.light.model` to pin one. Cursor's `auto` already picks per request. A
project overrides any entry, for example `ttp config <name> providers.claude.tiers.light.model opus`.
A task's own model or effort pin wins over its tier. A failed light try is retried at standard,
on the standard model.

## Jev (optional add-on)

Jev is a cheap outside decision model that screens watcher observations, picks task tiers and rates
coordinator turns. It is optional and off until you save a key (`ttp secret jev`); from then on it
is on in every project of yours on that machine (`jev.enabled` `auto`). While on, it sends log and
alert text, task specs and coordinator digests to an outside paid service. Turn it off for one
project with `ttp config <name> jev.enabled false`. Without a key, screening is rules-only.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, all of
  them until the last stretch before the line. When a plan is below its line, slots sit idle and
  nothing is queued, the coordinator is asked for more work, less often each time it finds none.
- Workers, reviewers, everything they start (detached checks, tests, reference models) and the
  daemon's pushes run at low CPU priority, 10 nice levels below the daemon (`runner.nice`, 0-19;
  0 = normal priority), so they never slow the machine's own work. The daemon and the coordinator
  keep normal priority. Each run's `exit.json` records the level its agent ran at.
- On macOS, while the project has work (a running run or push, or queued work that can start now)
  and the machine is on AC power, the daemon keeps it from idle and maintenance sleep with
  `caffeinate -i -s`, tied to the daemon's process so it ends with it. A closed lid without an
  external display, or a sleep you ask for, still sleeps the machine; the harness never changes
  `pmset` settings. It lets go when the work ends, on battery, or with `runner.prevent_idle_sleep` set
  to `off` (`auto`, the default, is on for a Mac; other platforms never hold one). `ttp status` and
  the web app say when it is held, or why not while there is work, and say "the lid is closed: work
  pauses while the Mac sleeps" while a closed lid will sleep it.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved. `ttp lock` waiters take turns in arrival
  order; a nested `ttp lock` of a resource the command already holds passes straight through; inside
  a run a wait is capped at half the run's stall limit (not in a `ttp detach` job or a background
  driver that outlives the run), then exits 75 so the task can wait on `ttp lock --probe <resource>`
  (0 free, 75 busy or paused) without a model. Config `device.locks` (opt-in) names one device by several names: all share the first name's lock and pause, tasks that use one
  get the `needs_device` label, and at most `device.max_tasks` of them run at once. A worker starts
  a long job with `ttp detach <name> -- <command>`; a run that ends without a hand-off, or waiting
  without a `retry_when`, then waits on `ttp detach --check`, which exits 0 once each job wrote exit
  code 0, 4 once all ended but one wrote another exit code or is gone without one (it prints `job
  finished rc=N` per job), and 1 while any runs. On 4 the daemon never treats the wait as passed: the
  hand-off's `on_pass_cmd` decides model-free, else the task wakes at once at its own tier with that
  output; a `start_when` on it raises `deferral_job_failed` instead of starting. Other probes on it (a `when:` hold, an `until_probe`, a pause or mute `end_when`, a machine change's `--until-probe`) take 4 as over and say the job failed. `ttp detach` records what the job asks for (`--cores`, `--devices`,
  `--mem-gb`, else `-j N`-style flags in its command); the daemon samples the CPU time of the job's own
  process tree (Linux /proc, macOS ps; device files from `device.nodes` on Linux), and a job using under
  a quarter of its cores, or none of a held device, for `waiting.job_use.window_s` raises one
  `job_underuse` event and brings its task's next check within `check_cap_s`. `ttp status` and the web
  app show requested vs used per job ("unmeasured" where it cannot sample). `ttp detach --remote <ssh alias> [--dir <dir>] <name> -- <command>`
  starts the driver on another host with setsid nohup (default folder `~/.ttp-detach/<run>`, with its
  .log, .rc, .pid, .boot and .start); its probe `ttp detach --check --host <alias> <dir>/<name>` exits
  0 once the .rc holds 0, 4 once it holds another exit code or the driver is gone (pid dead, or that
  host's boot id changed), 1 while it runs and 255 while ssh fails. Started inside a run, its probe
  carries `--mirror <run folder>`: each answer copies the job's exit code and the last 64 KiB of its log
  back as `<name>.remote.rc`, `<name>.remote.log` and `<name>.remote.json` (the tail is sent only when the
  log grew), and an exclusive run's hold keeper refreshes them as it probes. Once an end was mirrored, a
  host that stops answering (it reset, or the network went) still yields 0 or 4, not 255. A driver gone without an exit code
  reads as died with the host reboot (the boot id recorded at launch changed), crashed or killed (same
  boot) or unknown (no boot id readable); a local job's check says the same. Long device runs append
  and fsync each step's result as the step ends, not only in an end-of-run summary file; with `--rows <file> --after <n>` it
  exits 0 also once that file (on that host) has more than n lines, and 0 once the job ended or its driver
  is gone, else 1. A retry_when that tests only an end marker over ssh is flagged. `ttp ssh-preflight <host>` exits 0 once the host answers over ssh
  and 255 while not (12 with `--classified`), 10 when its host key does not verify and 11 when
  authentication fails (ssh runs with strict host-key checking and no connection sharing);
  `ttp ssh-preflight @<resource>` looks the host up in config `device.hosts`
  ({resource: host}, else `device.runners.<name>.host`) each time it runs, so a waiting probe follows
  a resource moved to another box (an unknown resource exits 2). Config `device.runners.<name>` (opt-in) sets up one serial
  device-job runner per device host instead of a driver per task: `host` (ssh alias; empty = this
  machine), `dir` (its state folder there), `health` (a command that must pass before each job),
  `drop_check` (a command that tells a device drop from a plain failure), `max_drops` (default 2),
  `health_wait_s`, `job_timeout_s`, `reservation_cap_s` and `idle_exit_s`. With `reservation_cap_s`
  (the box's longest allowed device reservation) submit refuses a job whose limit is above it, a job
  queued before the cap was lowered is failed without being started, and a job with no limit of its
  own runs under the cap. `ttp devq` installs the
  runner on the host under a temporary name and renames it into place, never over a running copy; a
  runner stopped with TERM ends its health or drop check with it (jobs keep running and the next
  runner adopts them). A task queues each device job with
  `ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] -- <command>` and waits on
  `ttp devq probe <runner> <id>`, which exits 0 once the job's done marker is written (done, failed,
  skipped or wedged, with exit code and log), 1 while it waits and 3 while it waits with no runner alive:
  the project's daemon then runs `ttp devq start <runner>` itself, without a model, at most every
  10 min and `device.runner_restarts_per_day` (default 6) times a day per runner, and queues one
  harness task to fix the runner after two failed restarts in a row or the day's cap. The runner is
  started under a lock (a second start does nothing), runs jobs in arrival order, runs a job again
  after a drop or a host reboot, and skips a config that dropped `max_drops` times in a row until
  `ttp devq clear <runner> <config>`; drops go to its `drops.log`. `ttp devq status` shows the queue.
  While a job runs, the runner reads its output for `wedge_patterns` (regexes; the default list holds
  the fatal hang signatures tt-metal and tt-umd print, such as "potential hang detected", "the device
  is unrecoverable", "the board should be reset" and "Timeout waiting for ARC msg request queue"). On
  the first match it stops the job (TERM, then KILL after 5 s), frees the device for the next job and
  writes the marker with status `wedged`, the pattern and the line, so the task can recover the device
  instead of waiting out the time limit. A job's `--wedge-pattern <regex>` (repeatable) replaces the
  list for that job and `--no-wedge-check` turns it off.
  Every job carries a build stamp, taken on the runner's host from the job's `--workdir`: the repo
  (origin without credentials), branch, commit, a dirty flag with a hash of the uncommitted changes,
  and the built artifact (`--artifact`, else the runner's `build_artifact`) with its size, time and
  sha256. Submit prints it and warns on a dirty tree or on changed C/C++ sources newer than the
  artifact; the job record and the done marker keep it under `build`, the runner stamps again at
  start and records any difference under `build_changed`, and the probe prints it.
  Before it queues a job, `ttp devq submit` lints its command so a job that would fail at once never
  takes a slot: it refuses a command `bash -n` rejects and a script the command calls that is missing
  on the runner's host or not valid shell there (checked over ssh in batch mode, read-only; an
  unreachable host only warns), and warns when a script path, the job id or the workdir names a task
  other than the submitting one. `--no-lint` skips the check.
  An opt-in job guard, run by the host at submit, keeps a job from harming the host (each part is off
  until set; `--no-lint` does not skip it): `script_lint` (`"warn"` or `"refuse"`) checks the command
  and the shell scripts it calls for process-group isolation (`setsid`, plus traps on EXIT, TERM and
  INT that kill the process group) and for a `df` or `du` guard before downloads or cache writes;
  `netfs_prefixes` (network-filesystem mount prefixes, none by default) flags weight and cache paths
  that resolve onto one, symlinks followed without touching anything under the prefixes
  (`netfs_policy` `"warn"` or `"refuse"`); `disk_max_pct` refuses a submit, and holds queued jobs
  like a failing health check, while a disk in `disk_paths` (default `/` and the runner's folder) is
  fuller. A job may declare `--input <path>` (repeatable; must be readable on the host) and
  `--cold-start <s>` (a warning when it is longer than the job's limit).
  `outside_user` (a regex for the command line of an outside device user to wait for, such as a
  script that drives the device outside the runner) closes the race between the two: while one of
  the user's processes matches it, the runner does not start or start a job, and the probe keeps a
  task with a pending job waiting until it ends.
  Config `runner.device_timeout_max_s` (opt-in) caps device-job timeouts: `ttp devq submit` refuses a
  longer `--timeout` and gives a job without one the ceiling when the runner's `job_timeout_s` is
  unset or above it; each job sees its limit (also under a runner's `reservation_cap_s`) as
  `TTP_DEVQ_TIMEOUT_S`, and workers are told to keep
  every device timeout (broker jobs, driver steps) within the ceiling and split longer runs.
  Locks and pauses are per project; a
  resource that several projects on one machine use is declared shared (`shared_resources` in the
  config, or `ttp machines add <alias> --shared [names]`), and then all of them take turns on its
  slots, a pause of it holds in each, and `ttp status` shows which project holds or paused it.
  Each project still gives its own slot count (`resources`); when they differ, every project
  uses the smallest, and `ttp status` and the coordinator say so. A count lowered while a holder
  keeps a higher slot applies to the next holders. A pause outlives the resource leaving the
  share: `ttp machines` refuses to unshare or remove a paused one, and a project that drops it
  from `shared_resources` keeps the pause as its own.
- Each task edits only its own worktree. A code task branches from the first of:
  `delivery.base_ref`; `delivery.push_branch`, then a branch the charter names as
  `branch <name>`, each only if it exists here or on origin; the remote's default branch
  (`origin/HEAD`); the checked-out branch.
  Other kinds run in the project root unless `worktree.kinds` lists them (say `["work"]`); a run
  that leaves the root on another branch or with new uncommitted changes raises an alert naming it.
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. A
  checked-out branch the remote already has (published with `ttp push --own`) is never rewritten:
  the rebase runs on a detached copy and the branch stays as it was. It exits 4
  when the rebased change edits a plugin under `plugins/<name>/` but a manifest keeps the version
  already on the branch (two batches that bumped to the same version). The target
  is `delivery.push_branch`, which must be set explicitly; it refuses without one, without
  checks unless the change touches only docs (`*.md`, `*.rst`, `docs/`, ...), when
  `delivery.push_allowed` is false, and for `HEAD`, `main`, `master` or the
  remote's default branch. Pushes to one branch take turns under a lock that a killed push or a
  reboot frees. One that waits longer than `delivery.push_wait_s` exits 75 and prints a
  `retry_when` for its hand-off: `ttp push --free`, which exits 0 once the turn is free. Unset,
  the wait is twice the last measured check run plus 60 s, at least 900 s and at most 2 h.
- A check is a command, which runs on every head. One that only makes sense once a file exists
  (a test file a later change adds) can opt in to being skipped where it does not apply:
  `{"run": "pytest tests/test_new.py", "if_exists": "tests/test_new.py"}`. `if_exists` is a
  repo path or glob (a directory counts by its files) looked up in the commit being checked. A
  skipped check is logged as `skipped (not applicable: ...)` by `ttp push`, the push queue and
  `ttp checks` (in `checks.log`), and never counts as passed: when every check is skipped the
  push or `ttp checks` fails. Plain string checks never skip; `after_push` takes the same form.
- A check that covers only some paths can be scoped by them:
  `{"run": "pytest -q tests", "if_changed": ["src/**", "tests/**"]}` (one glob or a list, matched
  as `delivery.push_exclude_paths` is). It is skipped, and logged as `skipped (not applicable:
  ... changes nothing under ...)`, when the diff from the push target to the head being checked
  (`git diff --no-renames <target>...<head>`, so a moved file counts under both names) touches no
  matching path, and runs normally otherwise. The push target is the fetched tip for `ttp push`,
  `ttp push --own` and the push queue, the pushed batch's previous tip for `after_push`, and the
  local copy of `delivery.push_branch` for `ttp checks`; with none to compare with, the check
  runs. A diff git cannot give (a missing ref, no merge base) fails the check. A head on which
  every check is skipped this way touches nothing any check covers, so it goes unchecked, as a
  docs-only change does without checks: a notes-only branch passes `ttp push --own` without a
  catch-all check. If any of the skips came from `if_exists` instead, nothing checked the head and
  it fails as above. A check may set both keys; outside its declared scope it skips, but inside
  that scope a missing `if_exists` target fails rather than leaving the change unchecked.
- Config warnings flag checks that pin a full commit SHA, use an absolute temporary environment,
  or name a target missing from the branch tip without `if_exists`. Declare applicability with
  `if_exists`/`if_changed` and retain coverage for the code the check protects. Warnings do not skip
  commands. `ttp checks` records a missing-target failure only when the target is absent from the
  tested Git tree (Python cannot open it, pytest cannot find it, or collects no tests). A check-only
  review failure on an unrelated change becomes a wait with one harness repair task per check
  signature, rather than blocking its dependents. Changing `delivery.push_checks` reruns those
  checks without a model. Related changes still fail; missing scope or unreadable history is treated
  conservatively. Without explicit scope, a nested component test covers its component and root
  `tests/` covers all code. A retry never approves code unless the reviewer supplied an `on_pass`
  verdict and the unchanged, clean commit passes its checks. A mandatory selector with no case
  for HEAD can report `check selector: no case for HEAD <40-hex>` (or `no matching case`). A
  nonzero check with that exact tested commit gets the same single repair owner and waiting
  dependents. Its repair retains the selector and every mandatory check; a previous head's pass
  or byte-equivalent tree never approves the unmatched head.
- Checks run with `TTP_PUSH_MODE` set to `target` (`ttp push` or the push queue, to
  `delivery.push_branch`), `own` (`ttp push --own`) or `checks` (`ttp checks`), and `TTP_PUSH_TIP`
  set to the tip of the branch pushed to as fetched before the checks (empty for a new branch and
  for `ttp checks`). A check that should only gate the push branch tests
  `[ "$TTP_PUSH_MODE" != target ] || ...`.
- `ttp checks` runs, and the checks of `ttp push` and the push queue, share a host-wide cap, across
  every project on the machine: each takes one of
  a few slots under `~/.tt-project/check-slots` before its commands run, and a run over the cap
  waits its turn in arrival order. The wait shows in `ttp status`, the task's notes and the
  `--result` probe; it never fails the checks. A foreground `ttp checks` (a tool call) waits a
  minute at most: then it starts the same checks detached, in the same place in the queue, prints
  their `retry_when` and exits 75 (with no project it waits on in the foreground).
  Slots are OS file locks, so a crash or reboot frees them. Workers' sandboxes may write the slot
  directory; where it cannot be written, the checks stop with an error naming it (exit 2; a push
  does not go) instead of running uncapped. The default is one slot per 16 cores (at least one); set it with
  `ttp config --account checks.host_max N` (0 turns the cap off) or `TTP_CHECKS_HOST_MAX`. A
  `ttp checks` or push inside a check command does not wait for its parent's slot. Checks that
  gate a push to the push branch (the push queue's batches and `ttp push`) hold the push lock, so
  they never queue behind task checks: each push branch has one priority slot of its own beyond the
  cap, shared host-wide across projects. Task checks keep their cap and their order. The batch log
  and `ttp status` say when a push holds it (`push priority slot: ...`); a killed push frees it too.
  `ttp push --own` publishes a task's branch and queues like task checks.
- Shared check failures: a task waiting on its own detached `ttp checks` that failed only on pytest
  ids (the failing command's `FAILED`/`ERROR path::name` lines, complete against pytest's summary)
  that also failed in at least `checks.shared_min_tasks` (default 2) other changes' checks on other
  heads (one change's code, review, review-fix and `continue:` tasks count as one; its own never count) within `checks.shared_window_h` (default 6), or on the push queue's known-bad tip, is held
  asleep instead of woken at its tier. Its checks run again on the same head, model-free, once the
  push branch's tip moves past a commit changing a failing test's file, or after
  `checks.shared_backoff_h` (default 2); after 3 such reruns it wakes as before. Each shared failure
  is one incident with one fix task and one low feed line; the daily audit lists the tasks held and
  the wakes avoided with their estimated $. A failing id whose earlier incident's fix task ended done
  and landed through the push queue within `checks.shared_repeat_h` (default 72) makes a repeat: its fix task runs at standard and must give
  the root cause, use a fake clock or event in place of wall-time assertions, and pass 20 runs of
  each repeated test under parallel load at the default worker count before done. Any other runner, a cut list or one failing id not seen
  elsewhere wakes the task as before. A task still lands only after its own checks pass.
- Checks receive a writable, absolute `TTP_RUN_DIR`, never empty or `/`. Foreground `ttp checks`
  uses the worker's run directory. Detached checks, pushes and push-queue batches each
  get a unique directory under the project's `state/check-reports/`, outside the checked worktree.
  Write reports there, for example `echo passed > "$TTP_RUN_DIR/x"`. Outputs survive worker cleanup
  and remain after completion. Their `TMPDIR`/`TMP`/`TEMP` scratch lives separately under
  `state/check-runs/`; the whole allocation is removed after its owner and last command exit
  (or by later recovery). Git discovery in scratch stops before reaching the enclosing project,
  while discovery in the checked worktree works normally. Scratch and lifetime locks never
  follow symlinks. `checks.pid` and push queue markers record `check_run_dir` and `check_report_dir`; queued launches
  rebuild missing directories from project state after a daemon restart. A rebooted check invocation
  gets a fresh directory. An invalid or unwritable output directory fails before any check runs.
  Foreground pushes keep progress notes and lock-wait accounting in the worker's original run directory.
  Detached launches do not inherit the worker's `TTP_PIDNS` or `TTP_CGROUP`.
- `delivery.push_exclude_paths` (globs, off by default) keeps files off the push branch, such as
  task notes or `tmp/`: `["tmp/**", "notes/*.md"]`. `ttp push` and the push queue refuse a change
  when any commit it brings adds or modifies a matching file, and name the files; a commit that
  deletes one passes. A pattern matches a path, a directory by its files, or as an fnmatch glob
  whose `*` also crosses `/`. `ttp push --own` is not affected.
- Each check (in the push queue, `ttp push` and `ttp checks`) runs with first-failure semantics (bash `-e -o pipefail`, `sh -e` without bash), so
  `a; b` or `a | b` fails when `a` fails. Still prefer one required command per entry. A coverage
  map keyed by full commit SHA must reject unknown heads and fail when a required input is missing;
  never skip a check because of an earlier pass or failure, and add a negative control that must fail.
- A harness selector must not need its own future HEAD in a publication whitelist. Use
  `ttp check-plan --purpose harness --definition-commit <full-commit-id> --definition-path checks/harness.json`
  as a mandatory check command, run from anywhere in the repository (it resolves the top level). The independently reviewed JSON
  definition is read from that exact commit, never from its working copy. For example:

  ```json
  {"purpose": "harness", "coverage": {
    "paths": {"checks/selector.py": "<64-character SHA-256 of committed file bytes>",
              "tests/test_selector.py": "<64-character SHA-256 of committed file bytes>"},
    "commands": ["python3 -m pytest -q tests/test_selector.py"]
  }}
  ```

  Bind every input the commands rely on, including imported helpers and fixtures. Paths are
  exact, repository-relative regular files; they are read literally, without glob expansion.
  Each must exist at the checked HEAD, match its digest, and match the working file used by the
  command. A different HEAD with those covered inputs runs the same required commands; this
  says nothing about publication approval or files outside the binding. The command checks the
  HEAD and bound inputs again after each successful command. Commands use the usual check shell
  and venv selection, run in order, stop at the first failure, and never skip or reuse a pass.
  Its own errors exit 65 (identity or coverage), 66 (input or definition) or 20 (git) and print a
  `check-plan:` line on stderr; a failing command keeps its own exit code, so a command that itself
  exits 65, 66 or 20 is told apart only by that stderr line, not by the code.
  Prepare the inputs first, then commit the definition and have its purpose, coverage and commands
  independently reviewed; installation pins that definition commit. There is no self-referential
  requirement to include the definition's or a later selector-preparation commit's own SHA.
  When a separate pinned review is more than a harness needs, `--definition-at-head` (purpose
  harness only, in place of `--definition-commit`) reads the definition from HEAD's own commit, never
  its working copy: a change to a covered input and its refreshed digest land in one commit, with no
  second commit to move the pin, and the change's own review covers the definition. A covered input
  whose digest was not refreshed still fails, and the error prints its digest at HEAD.
  A harness definition may bind a path to `"committed"` instead of a digest, for an input that changes
  often and is checked by its own commands (a watcher's or schedule's JSON): whatever HEAD commits there
  is covered, under a pinned or an at-HEAD definition, with no digest to refresh and no pin to move.
  The file must still exist at HEAD as a regular file and match its working copy. Candidate
  definitions keep exact digests.

  A separate `--purpose candidate` definition keeps exact publication identities:
  `{"purpose":"candidate","candidates":{"<full-candidate-commit-id>":{"paths":{...},"commands":[...]}}}`.
  Each candidate uses the same paths/commands binding; unknown candidate HEADs still reject.
  Purpose must match the committed definition and is never inferred from the current HEAD,
  push mode or an environment variable. Keep all existing mandatory candidate gates alongside
  the harness check; installing harness coverage does not waive them or carry earlier approvals
  to a changed definition. Applicability-gap ownership and retries remain independent of this binding.
  Exit codes are **65** for unknown identity, changed coverage or purpose mismatch, **66** for
  missing/invalid required input or definition, and **20** for Git failure even if Git also prints
  a matching identity. A selected command's nonzero exit code is preserved (for example **42**);
  **0** means every selected command passed under the binding.
- Tag a check entry `"purpose": "harness"` (only that `ttp check-plan --purpose harness ...` command) or
  `"purpose": "candidate"`; untagged entries serve both. `ttp checks --purpose harness` runs the harness and
  untagged entries plus the commands after `--`, lists candidate entries as `not run: candidate publication
  gate`, fails (exit 1) unless a harness entry ran or reused a recorded pass (one skipped by `if_exists`
  or `if_changed` does not count), and records a pass that never counts for a PR, an `on_pass` hand-off
  or `ttp push --own`, and opens no check gap. The candidate `ttp checks` (a code task's default) and
  `ttp push --own` skip harness entries and report them skipped, never passed: the plan belongs to the
  harness's own repo, not to the candidate's. `ttp push` and the push queue run every entry. Started
  outside the repo that has its definition commit (or outside any git repo), `ttp check-plan` exits 66
  and says so, naming the project's own repo when that one has the commit.
  With no harness entry configured, `ttp checks --purpose harness` runs a default harness plan, all tagged
  harness: `git diff --check` from the push target (else HEAD's parent) to HEAD, leaving out `*.log`, and
  `python3 -m pytest -q runtime/tests` and `bin/tests`, each only where that folder exists (`if_exists`:
  a missing one is reported skipped, never passed). A configured harness entry replaces the whole plan.
- `ttp checks` refuses a worktree with uncommitted changes, except unstaged edits to tracked files the
  repository's ignore rules match: a log a running watcher appends to (add it to `.gitignore`) no longer
  forces a fresh worktree. The run lists them; a staged edit to such a file still blocks.
  `ttp checks --purpose harness` also tolerates unstaged edits to harness state no worker's change
  owns: `*.jsonl` records and files under a `state/` directory, and in the project's own harness repo
  `project.json`, `schedules.json`, `CHARTER*`, `MEMORY.md` and `memory/`. It lists them; any other
  uncommitted edit, or a staged one, still blocks.
- tt-project runs no whitespace check of its own. A `git diff --check <base> HEAD` in
  `push_checks` also fails on committed run logs, whose captured lines keep trailing whitespace.
  Leave them out with a pathspec: `git diff --check <base> HEAD -- . ':(exclude)*.log'`. Files
  that `.gitattributes` marks `-diff` or `binary` are skipped by git already. Checks stay as
  configured; `ttp doctor` names a `git diff --check` that does not leave out `*.log`.
- A bare pytest in a check (`pytest`, `py.test`, `python3 -m pytest`, also after `env` or
  `timeout <n>`) runs as `<venv>/bin/python -m pytest` when the repository has a `.venv/` or
  `venv/` with pytest in it (in the worktree, else in its main checkout, else the active venv: a
  run's is the project's, and `ttp checks` activates the project's venv when none is). The main checkout's venv
  is not used when it has the project installed editable from the main checkout: a worktree's tests
  would import that code and could pass falsely. The check then runs as written and its output says
  why. A check that fails at import because a package is not installed is reported by `ttp checks`
  and `ttp doctor` as an environment problem, not as the head's code (as an environment problem
  also, when tests failed too): point the check at a venv. Before it runs checks, `ttp checks` warns
  of a venv whose `bin/python -c pass` fails (a dangling `.venv` link, or a venv made on another OS)
  and prints the command that makes it again.
- Fast path (opt-in, off by default): with no `delivery.checks_fast_globs`, every diff runs the
  project's full checks. A project that lists globs there (for example `["docs/*.md"]`) gets the
  fast path: when every changed path matches one and none matches `delivery.checks_fast_exclude`
  (by default code, scripts, manifests, tests, templates and skill/prompt files; a list replaces
  it), `ttp checks` runs only the whitespace and CRLF checks and says so. That pass is not a full
  pass: it never counts for a PR or `on_pass`. `ttp checks --full` runs the project's checks
  anyway (it is kept when the checks are queued or relaunched after a reboot), and a full pass
  already recorded for the tree is reused. A scoped `push_checks` entry that applies also turns it
  off. Known gap: a project that opts in owns its globs. A glob that covers installed or
  test-asserted content (for example a plugin README that tests read) skips the checks those
  changes need; list only paths nothing installs or tests.
- `ttp checks -- <cmd>` adds a check. Several words run as that argv (`ttp checks -- pytest -q`);
  one quoted string runs through the shell (`ttp checks -- 'FOO=1 pytest -q && ruff check .'`);
  several quoted strings run as one check each (`ttp checks -- 'pytest -q' 'ruff check .'`). An
  added check that `delivery.push_checks` already runs on every head, or through a scoped entry
  that applies on this head, is dropped, and said, so it never runs twice. Spellings of one pytest
  run (`pytest`, `python3 -m pytest`, `<venv>/bin/python -m pytest`) count as the same command.
- In Claude Code workers and reviewers, a Bash call that runs one of the configured pytest checks
  in full (every path it names, or a directory above them, and no `-k`, `-m`, `--lf` or node id
  it does not have) is refused with a pointer to `ttp checks`, which reuses a recorded pass.
  Focused runs pass. `TTP_ALLOW_FULL_SUITE=1` before the command (or in the run's environment)
  lets one through; each refusal is a line in the run's `refusals.jsonl`.
- Scope a mandatory check by what it covers, never by whether it would pass. Prefer `if_changed`
  on the paths the check covers, or `if_exists` on the file it runs. Where no file marks the heads a check applies to (say a test that
  is missing at an independently reviewed head of another delivery branch), the fallback is a
  shell conditional on the full SHA in the check itself:
  `h=$(git rev-parse HEAD) || exit 1; [ "$h" = <full 40-character sha> ] || pytest -q tests/test_x.py`.
  A leading assignment such as `h=$(...)` is accepted as a check's first command, like `set -e`.
  Assign first, then compare: `set -e` does not catch a failed command inside a substitution
  in a test, so `[ "$(git rev-parse --verify HEAD)" = "$expected" ]` just compares an empty
  string when git fails. Write `head=$(git rev-parse --verify HEAD) || exit 1` and then test
  `"$head"`.
  Write the condition so that an error in it runs or fails the check, never skips it. Changing
  a check's scope changes what a head was checked against: every head it affects needs a fresh
  review, and earlier approvals do not carry over. Never add blanket "skip if the test file is
  missing" guards (`[ -f tests/test_x.py ] || exit 0`, `|| true`): a missing test, a failed
  assertion and a git error (an unset identity, a bad ref) must still fail the check.
- `ttp push --detach` runs the same push in a process of its own and returns at once, for
  checks that outlast a worker's tool call. It prints a marker under `state/pushes/` and a
  `retry_when` probe, `ttp push --result <marker>`: 1 while the push runs, 0 once it finished,
  printing `ttp push: pushed <sha> to <branch>` (and version) or `ttp push: not pushed (...)` and the log tail. For an `on_pass_cmd`, add `--pushed`: exit 0 only if the push landed, 1 while it runs, 3 if it finished without pushing; no grep needed. A push killed by a
  crash or a reboot frees its locks; the probe (or the daemon) then records it as failed, saying
  whether it died at startup, in a reboot or later, instead of waiting. From a sandbox that runs
  each command in a PID namespace of its own (Codex on Linux), which kills a background process
  when the command ends, the push is queued and the daemon starts it; the probe exits 1 meanwhile.
- `delivery.version_bump` lets `ttp push` own the version bump, so parallel reviews never race for
  one version: `{"files": [...], "changeset_dir": ".changeset"}` (optional `package`, and `paths`,
  default the folder the files share). After each rebase, when the change touches `paths`, it
  sets every listed file one patch version above the tip's and adds a changeset when the change
  brings none, in one `<package>: X.Y.Z (<subject>)` commit; a later round replaces that commit.
- `delivery.push_queue: true` hands pushing to the daemon. A review that passes approves the
  exact head it reviewed (`"push": [{"branch", "head"}]` in its hand-off) and becomes `pushing`; it
  never runs `ttp push`. Without a model, the daemon replays the approved changes in order onto the
  latest tip of `delivery.push_branch`, adds one version bump and changeset for the batch, runs
  `delivery.push_checks` once and pushes without force. Then the reviews are `done`. Conflicts
  that need no judgment are settled in the batch and judged by those checks: version lines, lines
  both sides added at one spot, and changes of different lines that git calls a conflict only
  because they are adjacent. A change that really overlaps another goes back to its review to
  resolve the conflict; a failed check fails the
  review and wakes the coordinator. The batch finds the first change that fails by checking
  prefixes, pushes the ones before it, and checks the ones after it once more without it, pushing
  them too when they pass. Keys, all under `delivery`: `push_batch_s` (default 900: a
  batch starts once the oldest approval waited this long; 0 = at once), `push_batch_max`
  (default 8: start at once with this many; also the most one batch takes), `push_min_gap_s`
  (default 1800: a batch starts only once the last one ended at least this long ago, measured
  from its recorded end so a restart or a failed batch never stretches the wait; a full batch, a
  priority-1 review's approval, or one the last batch requeued unchecked after a failing change
  starts at once; 0 = off), `push_solo_paths` (default `["plugins/tt-project/template/prompts/*.md"]`:
  git pathspecs a batch takes at most one approved change touching, as two prompt changes that
  each fit a size cap may not fit together; the others wait for a later batch; `[]` = off),
  `push_cap_tests` (a regex of prompt size cap test names, default `(?i)prompt\w*_(?:stays_small|under_its_cap|size)$`; the `$` anchor means a
  parametrized cap test id (`name[param]`) needs a custom `push_cap_tests`:
  when only such tests fail on a change merged in a batch, its review runs again as a light
  trim-to-fit fix told each measured overshoot, at no review attempt, at most twice per change,
  instead of failing), `push_landing_alert_h` (default 4:
  when approvals have waited this many hours with nothing landed on the push branch and at least
  two batches tried them, one high alert says so, with the attempt count and how many a host
  reboot cut short; it clears when a batch lands; 0 = off), `after_push`
  (commands in the same form as `push_checks`, for example a deploy) and `after_push_timeout_s`
  (default 1800). A batch that ends without writing an outcome is retried with a growing pause,
  and three such batches in a row raise a high alert, unless the host rebooted while it ran: then
  it is reported as cut short by that reboot (its time in the home zone), counts no try and no
  pause, raises no alert by itself and runs again as soon as the daemon is back. `after_push` runs after each push in a clean worktree at the pushed commit,
  with `TTP_PUSHED_SHA`, `TTP_PUSHED_VERSION` (the version `version_bump` reads at the pushed
  commit; empty without `version_bump`), `TTP_PUSH_TARGET`, `TTP_PUSH_TASKS`, `TTP_PUSH_BATCH` and
  `TTP_PROJECT` set. A failed `after_push` alerts the coordinator but never fails the reviews:
  their change is already on the branch. A malformed `after_push` (a scalar such as `true`) counts
  as a failed one. After setup and upgrades, use `ttp deployed "$TTP_PUSHED_VERSION"
  --commit "$TTP_PUSHED_SHA" --after-push` as the final verification (after allowing new
  daemons to complete their first tick). This mode returns 0 for done or pending, and 1 for
  failed. A pending merge needs a live daemon, a harness task owner and a known takeover
  time; it emits one low note and records a model-free check in the deploying project's
  state, due five minutes after takeover. The original deadline survives retries and
  restarts; a new commit of the same version is a new merge with its own, except that a
  harness task already holding an earlier release's merge keeps that earlier deadline. Once
  the deadline passes (also when it is already past after a restart or a reboot), the
  deploying project's daemon runs the host-side takeover itself, `ttp upgrade <name>
  --take-over-from <task>`, which fences that task even while it still shows activity. It
  tries up to three times, five minutes apart, and the check closes only when that project's
  own daemon reports the release (output in `logs/takeover.log`). A live daemon
  whose files are already at the release but that has not restarted onto them (or not yet
  ticked on them) is pending too, owned by that daemon, and is re-checked after
  `delivery.deploy_restart_wait_s` (default 900). A daemon that
  stops or fails to come up (high), or an upgrade error for this release, alerts at the next
  probe (about a minute), not at takeover. A takeover still behind after its last try, or a
  restart still behind at its re-check, alerts at normal severity. A project behind is one
  ongoing alert (`deploy-behind:<project>`): each later release it misses updates that alert in
  place (the newest release, since when it is behind, why), and only a daemon going down pages
  again. The push queue sends no after_push alert of its own for that failure. The alert clears
  once the project's own daemon reports the release, and a recovered
  daemon returns to pending. Missing ownership/deadlines, current upgrade errors and daemons
  that fail to start remain failures; an older release's upgrade error does not count.
  Keep nonzero upgrade errors as failures in the deploy command; exit 75 means deferred,
  and needs this final verification. Ordinary `ttp deployed` remains a strict read-only
  probe for `retry_when`. It also runs, queue on or off, after
  each `ttp push` or `ttp push --detach` that pushed to the push branch (never after `--own`), with
  `TTP_PUSH_RECEIPT` instead of `TTP_PUSH_BATCH`: a receipt under the project's state records it,
  a detached push runs it once its outcome is written and its locks are let go, and the daemon
  turns a failure, a timeout or a killed run into the same alert; the push stands. Off (the default), each review
  pushes with `ttp push --detach` as above. `ttp status` shows one line about the queue (what
  waits, the running batch, the last push and its deploy), the web app a Push queue card, and
  `ttp push --queue` lists the entries, the last 7 days' conflict counts (conflicted, resolved in
  the batch, sent back) and the last 10 batches. Only a rejected push, a failed
  `after_push` and batches that keep dying reach the top section, and only while they last.
- A push queue batch records each check that passes, keyed on the checked head's git tree, the
  exact command and the environment it ran in (the interpreter and venv it uses, the active venv
  and the target tip). A later batch on the same tree, as after a reboot cut the first, skips a
  check recorded there as passed and logs `reused pass from <batch> (tree <short>)`. Only passes
  are recorded: a failed check, and one skipped as not applicable, always run again. A check that
  is one plain pytest run (no second pytest, and no `PYTHONPATH` or `PYTEST_ADDOPTS` of its own)
  also resumes: a bundled pytest plugin records each test file whose every selected test passed,
  and the next run of that check under the same key skips those files and runs the rest; files
  are skipped whole, never single tests, and pytest-xdist runs never resume. The batch never reads
  the passes `ttp checks` records. Records live under the project's state
  (`push_check_passes.json`, newest 200 within 14 days; `push_check_resume/`, newest 20 within 3 days).
- `delivery.backup_remote` (off by default) names a git remote that each finished code task's
  branch (`ttp/t<id>-...`) is pushed to under the same name, fast-forward only: never forced, never
  to `main`, `master`, the push branch or `delivery.base_ref` (a value naming one is refused). A
  push that is not a fast-forward is skipped with one observation. Setting it from chat needs the
  user's word. Separately, a hand-off that finds uncommitted changes to tracked paths in the
  project's main checkout records one observation naming them (once per set of paths); nothing
  there is committed or changed.
- `delivery.own_push` (`existing_only` when unset) says which remote branches besides the push branch
  `ttp push --own` may push to, and every push that publishes as with `--own` (a notes branch, a
  `pr_branch:` or `rebases:` task). `on`, set explicitly: the task's own or carried branch, created
  on the remote when new. `existing_only`: only onto a branch the remote already has; it never
  creates one. Unset reads as `existing_only` with one exception: a code task's `ttp push --own`
  still creates its own `ttp/t<id>-...` branch while `delivery.draft_prs` is on, the documented
  way a draft PR's branch is first published; a review, another task's branch, a notes branch or
  a push outside a run never creates one. So a review of a `pr_branch:` or `rebases:` task whose
  branch the remote lacks is review only, or approves into the push queue when it is on. The
  refusal names the setting. `off`: none; `ttp push --own` refuses and names the push queue, a
  `pr_branch:` or `rebases:` task lands on the push branch like any other change, and no spec the
  daemon writes (reviews, fixes, PR fixes, `review.auto_notes`) mentions `--own`. Set it `off`
  when the project may push to one branch only. An unknown value reads as `off`. The coordinator
  may turn it `off` itself; `on` and `existing_only` need the user's word. The default was `on`
  until reviews created remote branches in a project allowed one push branch only.
- `delivery.drafts_during_freeze` (on by default): opening and updating draft PRs needs no one's
  word, even under a project's code freeze. Off, for teams whose freeze also covers drafts: the
  coordinator asks the user before a draft PR under a code freeze, and the ask gate lets that ask
  through (an ask about a draft PR outside a freeze is still refused). Leaving draft always needs
  the user's yes, and human reviewers are never requested, whatever the setting.
- Change windows: the template charter's Policies line `- Change windows: none` means disruptive
  work (restarts, rollouts, anything that disturbs others) may run whenever it is safe, and waiting
  for a lull is never a blocker. A team that works in change windows lists them on that line
  instead (with their time zone): the coordinator then gives such work `start_after` or
  `start_when` for the next window, and an `idle_capacity` event never moves it outside one.
- `delivery.allow_protected_push_branch` (off by default) lets `ttp push` and the push queue
  publish to a push branch that is `main`, `master` or the remote's default branch. Off, such a
  push branch is refused, the config alert and `ttp doctor` say so, and every review is review
  only, never followed by a raw `git push`. On, the push checks, the rebase before the push and
  the no-force rule apply as to any branch. Only the user's word sets it.
  While it is off, refuses the push branch and an open ask names it (a setting hold), every task
  told to land work there waits blocked on that ask, attempts untouched, instead of running into
  the refusal: queued ones before they start, and blocked ones a worker handed off naming the
  setting. A review with a push step still runs, as review only, and hands off blocked to join
  them. The daemon releases them all together once the setting is on or the ask is resolved. When
  the charter says pushes there need no approval while the setting is off and nothing asks about
  it, the daemon itself asks the user once (a blocking restriction ask), once per push branch
  and charter wording, with no coordinator turn.
- `delivery.plugin_version_guard: true` (off by default) is for a repository of plugins whose
  installs upgrade only to a newer version: `ttp push` and the push queue then refuse a change that
  edits `plugins/<name>/` while a manifest there (`.claude-plugin/plugin.json`,
  `.codex-plugin/plugin.json`) keeps the version already on the push branch.
- `delivery.fast_forward_also` (branch names, off by default) keeps more branches in step with
  the push branch, such as a `main` that holds the last good state: after each successful `ttp push`
  or push queue batch, each listed branch on the push remote is moved to the pushed commit, never
  forced and only when its tip there is an ancestor of that commit. The result is read back from
  the remote and recorded in the push outcome, the probe and the landing notice as
  `ff <branch> <sha>` or `not ff <branch>: <reason>` (not an ancestor, missing, rejected). A
  `not ff` is a warning and never undoes the main push. The push branch itself and invalid names
  are refused. Like other delivery keys that widen where work lands, setting it needs the user's
  word, and a charter that forbids pushing to a listed branch wins: leave that branch out.
- `delivery.mirror_remotes` (off by default) sends each pushed commit on to other git remotes,
  such as a mirror of the repository elsewhere: a list of `{remote, branch}` tables (or
  `<remote>/<branch>` strings), e.g. `[{"remote": "mirror", "branch": "dev"}]`, where `remote` is a
  git remote of the project's repository. After each successful `ttp push` or push queue batch,
  each mirror branch is fetched; when its tip is an ancestor of the pushed commit (or the branch
  does not exist yet) that same commit is pushed to it, never forced. A mirror that has diverged is
  left as it is. The result is read back from the mirror and recorded with the
  `delivery.fast_forward_also` lines as `mirror <remote>/<branch> <sha>` or
  `not mirrored <remote>/<branch>: <reason>`; a divergence names both commits. A `not mirrored`
  is a warning in the feed (a push queue event, or a line in the run's notes for a worker's
  `ttp push`) and never fails or undoes the main push. Mirrors run right after the push and
  before `delivery.after_push`, which still runs as before. The push branch itself and invalid
  names are refused, and setting it needs the user's word.
- `delivery.auto_merge_repos` (`owner/repo` names, none by default) lists the repositories whose
  pull requests the project may merge itself. The coordinator sets it only in the turn that carries
  the user's own message saying so, and only adds repos; each change is logged as a `config_grant`
  event naming that message. A charter Policies line granting auto-merge does not turn it on by
  itself; the coordinator sets the key in the turn where the user says so.
- A plan task starts from what is already known: prior work, the organization's docs and chats
  through the connectors you have, available skills, and public work. Skill plugins it recommends
  can be enabled for the project's workers only (`providers.claude.plugin_dirs`).
- On Claude, the part of a worker's prompt that is the same for every task (rules, charter,
  memory) goes in the system prompt, so the next worker, of any kind, reads it from the cache. The
  kind's rules and the task itself go in the user prompt.
- `providers.claude.worker_isolation: true` starts Claude workers and reviewers without your own
  MCP servers, plugins, hooks and user settings; the project's `plugin_dirs` and its hook still
  load. In one measurement it cut a worker's first turn from about 38k to 23k input tokens.
  `ttp new` turns it on; projects created earlier keep it off until you set it.
- `providers.claude.mcp_servers: ["name", ...]` lists the MCP servers isolated workers still get.
  Each run copies just those entries from your Claude config (local, then `.mcp.json`, then user
  scope) into its own owner-only file in the temp directory, and deletes it when the run ends.
  Listing a server approves it for workers. A name your config does not define is skipped: the
  run still starts, and `ttp doctor` and a low alert name it. Servers that come from a Claude plugin
  are not in those files, so they cannot be listed; add the server with `claude mcp add` to list it.
- `providers.claude.disallowed_tools: ["mcp__<server>__<tool>", "mcp__<server>__*", ...]` lists tools
  Claude workers and reviewers may not call, on top of the tools the harness always denies them; a
  trailing `*` covers every tool whose name starts so, e.g. every tool of one server. Use it to keep
  workers off a server's private-data or write tools while they keep its other tools. A malformed
  entry is refused when set; one written into project.json by hand is skipped and named by
  `ttp doctor` and a low alert.
- `providers.codex.worker_isolation: true` starts Codex workers and reviewers with
  `--ignore-user-config`, so your `config.toml` (its MCP servers, plugins and hooks) stays out;
  sign-in still works. Codex coordinator turns always skip it, and your rules files too, where the
  build has those flags. A config that sets your own model provider, API or ChatGPT endpoint,
  login method or credentials store is kept, since runs need it.
  Codex has no per-run MCP list, so isolated Codex workers get no MCP servers of yours.

## Where things live

| Path | Holds |
|---|---|
| `<project root>/tt-project/` | everything for the project; ignores itself, so nothing gets committed |
| `…/harness/` | the project's own harness (git): charter, memory, config, prompts, runtime, and a `pytest.ini` that isolates its own tests from an enclosing repository's pytest config and `conftest.py` (an upgrade adds it unless the harness has a pytest config of its own) |
| `…/state/` | database, run directories, logs |
| `…/worktrees/` | one git worktree per code task |
| `~/.tt-project/` | per-user registry of projects, secrets (mode 0600), the `ttp` install, account-level settings (`settings.json`), the global spend cache (`global-spend.json`) |

Each project starts from this plugin's template and then improves its own harness from
experience. When a newer tt-project is installed (`ttp setup` from the updated plugin; the skill does
it on first use after an update), each project's daemon merges it into its own harness within an hour
and restarts, keeping running work. `ttp config <name> upgrade.auto false` turns that off;
`ttp upgrade <name>` then merges by hand. `ttp upgrade` exits 75 when it changed nothing and the
project finishes the upgrade itself (a merge conflict handed to its harness task, or retried by its
daemon under `upgrade.auto`, which retries only a newer version), so a deploy script can count it as
deferred; exit 1 is a real failure, such as a conflict nothing retries.
Most conflicts settle without a model: changes to different lines, additions at one spot, and a
local edit that upstream now makes too (comments and spacing aside: upstream's version is taken,
the project's stays in the merge's first parent; a line the project removed and upstream reworded
is a real overlap). A project's own rules in a template prompt belong
between `<!-- ttp:local -->` and `<!-- /ttp:local -->` lines (the prompt leaves those lines out):
an upgrade takes upstream's text and puts each block back where it stood. When a project only added
lines to a template prompt, the upgrade fences them that way itself. Only a real overlap, such as
the project and upstream rewording the same line, or each adding its own version of one line (the
same leading key, or most of the text alike), goes to a harness task.
The same conflict deferred past 48 h, or past two harness tasks that ended without landing it,
reaches the coordinator once as a normal observation, not once per deploy.

## Notifications

Alerts go to every attached chat, to the web app (one-click browser notifications), and, if you
install it, to a desktop notifier on your workstation that covers all your projects
(`ttp notifier install`; asked once per user, recorded with `ttp consent notifier yes|no`). Only decisions, reviews, merges, funds and outages notify by default.
Workers can read Slack links you paste, using your Slack connector if you have one.

## Security

- The web app listens on localhost with a per-project token. From another machine,
  `ttp web <name> --tunnel --keep` opens and keeps an SSH local forward to it. The first one is
  asked once per user (`ttp consent tunnel yes|no`, kept in `~/.tt-project/consent.json`); a kept
  tunnel already installed counts as a yes.
- Workers run with your permissions, in their own worktree; restrictions in the charter are
  passed to every worker and enforced by the provider where it can (for example, no web tools).
- Text from logs, issues and chats is treated as data, not instructions.
- Secrets are entered in a terminal (`ttp secret …`), never in chat, and never copied into a project.

## Maintainers

Design notes, invariants and how to add a provider: [docs/design.md](docs/design.md).

## Limits

- Codex and Cursor report tokens but no cost; their spend is estimated from a price table. Set
  your own rates in project.json as `"pricing": {"codex": {"<model>": [input, cached input,
  output]}}` in $ per million tokens (`"default"` covers other models; same for `cursor`).
- A Claude run cut off before its final report is estimated from its streamed tokens. Codex
  reports usage per completed turn, so its mid-run budget check sees completed turns only. Cursor
  builds that offer `--output-format stream-json` stream their progress: the stall guard sees it,
  and until usage arrives the budget check uses a floor from the text written so far. Older
  Cursor builds print nothing until the end, so set `stall_s` above their longest run. A run cut
  off before reporting usage is booked at the elapsed share of its budget (flagged estimated).
  A run that wrote no output and reported no tokens is booked at $0 (except on an older Cursor
  build, which prints nothing until it ends).
- Codex workers run in Codex's workspace-write sandbox, with the project's state folder, the
  repository's git folder and the shared lock folder (`~/.tt-project/locks`, for `ttp lock` on
  resources shared across projects) added as writable roots. Coordinator turns use its read-only
  sandbox.
- Codex and Cursor coordinator turns run from an empty scratch directory, so the project's
  AGENTS.md and rules stay out of them. Codex's shell and web search tools are off, and Cursor
  runs in ask mode, when the installed CLI offers those switches.
- Codex workers get the harness hook (coordinator updates handed over once, PR draft guard,
  full-suite and `ttp say` refusals) as per-run config, never by editing your Codex home. Codex
  runs such a hook only when trusted, so the run trusts it for that call alone, and only when the
  hook is the run's sole hook source: if your Codex home, the repository's `.codex/` folder or an
  enabled plugin may add hooks, the worker runs without it and reads `steer.md` between steps (an
  isolated worker skips your config, so only the home `hooks.json` and the repository count then).
  A build without hooks or the trust flag keeps that older behaviour. Tested live on codex-cli 0.160.
- Not on Codex: worker plugins (`plugin_dirs`). Codex loads plugins only from those installed in
  its home (`codex plugin add` copies them there), so a run cannot add one for itself without
  changing your Codex setup; plugins you install yourself load as usual.
- Not yet on Cursor: worker plugins, worker isolation, coordinator updates reaching a running
  worker, and isolation of coordinator turns from your own CLI config and MCP servers. Cursor
  enforces no `no_internet` restriction and has no plan-window meter; without ask mode it has no
  read-only mode either.
- Cursor has no reasoning-effort flag; tiers map to model names.
- Context compaction per tier (`budget.compact_window_tokens`) works on Claude Code and Codex.
- Long command output stays out of a worker's context: `ttp clip -- <cmd>` and `ttp checks` keep
  it in a file and show a test run's failures (else head and tail). On Claude Code, Bash output
  past `budget.bash_output_max_chars` also goes to a file. A worker whose run re-reads more than
  `budget.split_reread_tokens` of context is told once to hand the rest on as a follow-up; its run
  note records when, and `ttp stats` shows what steered runs re-read after it and how they handed off.
- `ttp killscan <script>` flags kills by name or pattern (`pkill`, `killall`, any `pgrep` or
  `pidof`, `ps | grep` feeding a kill) before a worker runs a script; `--shim <dir>` writes
  stand-ins for `pkill`, `killall`, `pgrep` and `pidof` that only log the call (`ps | grep` and calls
  by absolute path must be edited out). `pkill -P <pid>` is not flagged. A pattern kill in a script
  can match the worker's own tool shell.
- `ttp svcscan <script>` flags service unit changes before a worker runs a driver on a shared
  machine: any `systemctl` verb but a read-only query (`stop`, `mask`, `reboot`, `daemon-reload`,
  `set-property`, ... also under `sudo -n`, in Python argv lists and across backslash-continued
  lines) and `service <unit> stop|reload|...`. `--shim <dir>` writes stand-ins for `systemctl`,
  `service` and `sudo` that only log a change (only read-only queries such as `systemctl is-active`
  reach the real one; `sudo` runs nothing else). Calls by path (`/usr/bin/systemctl`,
  `./systemctl`) or over ssh are flagged as such: a local shim does not stop them.
- Resuming a lost run's session works on Claude Code and Codex; Cursor starts fresh.
- A laptop pauses while it sleeps. Use an always-on machine for round-the-clock work.
