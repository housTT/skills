# Run setup

## Interactive gate

Do not treat open files, IDE selections, shell history, environment variables or a previous run as
user confirmation.

Prefer the host's native structured-choice question tool when it is available (`AskUserQuestion`
in Claude Code or `request_user_input` in Codex when exposed). Use an actual tool call, not a
Markdown imitation of a form. Put the recommended option first, label it recommended and give one
short reason per option. If the tool supports only single choice, split a multi-select decision
into consecutive questions. If no such tool is exposed, show a short numbered list and say exactly
what reply is expected. Never invent an unavailable tool or stop merely because the rich UI is not
available.

Ask one decision per turn and stop for its answer. Skip values explicitly stated in the current
conversation. When discovery leaves exactly one compatible choice, state the choice and why it is
the only one, then continue without a redundant confirmation. Never ask the user to confirm the
whole configuration in one message.

### Setup order

1. Ask for the mode: **static**, **static + measured**, or **measured over an existing static
   run**.
2. Ask which model to analyse. Do no model-specific discovery before the answer.
3. Existing run (measured over an existing static run): ask for the output root, list the runs
   under `<root>/op-analysis/<model>/static/` with their `source`, `destination`,
   `tt_metal_commit` and date, and ask which one to extend. A run whose `run.json` has `targets`
   instead of `source` and `destination` predates this layout and cannot be extended; say so and
   offer a new run. Its commit, targets and entry test are fixed; go to step 10.
4. New run: ask which source ref to pin. A suggested ref is allowed, but it is not confirmation.
   Resolve it before any discovery so every path, skip marker and CI claim comes from the pinned
   SHA.
5. Ask for the source target (`p100` or `p150`): where the code is expected to work today.
6. Ask for the destination target: any other target from `<plugin-root>/references/targets.md`.
   Offer `quasar-port` only when the model directory has a Quasar port at the pinned SHA (a
   directory listing, not a source read). Neither target has a default; show each option's
   one-line question from targets.md. Targets are fixed before implementation or test candidates
   are presented.
7. Discover implementations compatible with the source target and show the table from section 1.
   Ask which implementation to analyse.
8. Rank only the whole-model test candidates for that implementation and the source target.
   Recommend the best candidate with a concrete reason, then ask which test to use. The measured
   source phase reruns this same test.
9. Ask for the output root, explain it cannot be inside the tt-metal checkout.
10. Measured modes: ask which measured phases to run (source device, destination TTsim) and check
    their preconditions now: a local device of the source architecture (probe through
    `tt-device-mcp`), a checkout at the run's commit (`same_commit.py` for an existing run; for a
    new run compare the pinned SHA with `git rev-parse HEAD` of the checkout and a clean
    `git status`), and for a Quasar destination a ready TTsim build (`ttsim.md`). Report what is missing before any analysis
    starts and let the user drop a phase or stop.

Do not inspect model source, create output or delegate tracing until the applicable setup is
complete. Candidate discovery may inspect directory structure, tests, skip markers and CI workflow
references at the pinned SHA, but not trace the model implementation yet.

## 1. Implementation and test

Ask which model. In the tt-metal checkout, find:

- every model directory (`git ls-tree -r --name-only <sha> models/ | grep -i <model>`),
- every implementation module (common, functional, interleaved, sharded, per-arch
  `blackhole/`, `wormhole/`, `t3000/`, `quasar/`),
- for each implementation, its whole-model tests by kind (below), with the workflow under
  `.github/workflows/` that runs each one, if any,
- the skip markers on each of them: unconditional `skip` and every arch `skipif`
  (`is_blackhole`, `is_wormhole_b0`, ...), with their reasons.

A whole-model test runs the model forward from the model input through every block to the head
output. Confirm it by reading the test body: it calls the model's top-level forward (directly or
through a test-infra `run()`), not one layer, block or stage. Component tests
(`test_<model>_attention`, `_layer`, `_encoder`, `_embeddings`, ...) are never candidates.

Candidates by kind:

- **static**: end-to-end PCC tests and demo tests.
- **static + measured**: the static candidates, plus perf tests (device-perf: the
  `command = f"pytest ..."` string in `*perf_device*.py` or `*device_perf*.py`; e2e perf such as
  `test_*perf_e2e*.py`) whose test body runs the whole model, so the same test serves both phases.

For static analysis, evaluate the implementation and entry test against the source target. When
the destination is `p100` or `p150`, record whether the entry test also runs there, but do not
require it. For `quasar-port`, discover the port implementation separately per the rule below. Do
not require the entry test to run on a Quasar destination. Do not offer incompatible primary
implementations as choices; summarize how many were excluded and make their details
available on request.

Show one row per compatible implementation: module path, target fit, candidate node ids, skip
markers and whether CI runs them on the relevant architecture. Never derive the implementation
from a proposed test. Recommend an architecture-specific implementation exercised by CI or a demo,
not a reference or functional version. If several remain, prefer full target coverage and runnable
tests over naming or directory proximity.

After the user picks an implementation, rank its tests in this order:

1. runnable on the source target;
2. explicitly exercised by CI on the relevant architecture;
3. a whole-model PCC/demo, or for static + measured a warm-capable perf/PCC test;
4. the least setup indirection and fewest unrelated parametrizations.

Present the top candidates with their node ids, relevant parameters, skip status, CI coverage and a
one-line recommendation reason. Never recommend a test skipped unconditionally or for a relevant architecture. If none is runnable,
say so and follow the temporary-test procedure instead of silently choosing another implementation.

### No whole-model test

If the chosen implementation has no candidate of the required kind, say so and offer a temporary
test. Show its full content and stop for approval before writing it. It must:

- call the same top-level forward and weight preprocessing as the model's demo or component
  tests, with their batch size, input shape, dtypes and memory configs; say where each value
  comes from;
- use the existing `device` fixture and device params of the model's tests;
- carry no skip markers and no PCC threshold of its own (a PCC print is fine).

Where it lives:

- **static**: only in the run directory as `temp_test_<model>.py`, written once the output root
  is confirmed; never in the tt-metal checkout. Read it from there; every other source is still
  read as `git show <sha>:<path>`.
- **measured source phase**: copied as an untracked file next to the model's tests in the tt-metal
  checkout for the Tracy run, so its conftest and fixtures apply. Write it warm from the start (two
  forwards with `op-analysis-start` / `op-analysis-end` signposts). Delete it from the checkout
  after the run and say so.

Record it in `run.json` as `"test": "<run dir>/temp_test_<model>.py::<test name>"` and
`"temporary_test": true`, and state in `summary.md` that the entry test is temporary and not part
of tt-metal.

If a Blackhole and a Quasar implementation both exist (for example
`models/demos/vision/classification/resnet50/ttnn_resnet` and `.../resnet50/quasar`) and the
destination is `quasar-port`, record the port module in `run.json` `port_implementation`. The
port only fills the `quasar-port:*` columns of the source rows; it never adds rows.

## 2. Pin the source

Record the full SHA of the commit analysed (`git rev-parse <ref>`; prefer a freshly fetched
`origin/main` unless the user names a ref). Read every file as `git show <sha>:<path>`; never read
the working tree, except an approved temporary test (section 1). For Quasar, record the ttsim
version if the user knows it.

zsh trap: `$VAR:path` applies the `:t`/`:h` modifiers. Write `<sha>:path` with the literal SHA or
use `"${VAR}:path"`.

Treat `main`, `origin/main` and `HEAD` as different refs; never silently substitute one for another.
When the user says `main`, resolve local `main` exactly, and report whether an already-present
`origin/main` points elsewhere and which is newer by ancestry. Fetch only when the user selected a
fresh remote ref and the environment permits it. Record both the symbolic ref and resolved SHA in
the run notes so a later comparison can detect a stale or divergent pin.

## 3. Output location and previous runs

Root: current working directory unless the user names one; never inside the tt-metal checkout.

- Run: `<root>/op-analysis/<model>/static/<YYYY-MM-DD>_<sha7>/`. Measured phases write into the
  same directory, so one run holds the static tables and the measured data on top of them.

`run.json` fields:

```json
{"kind": "static", "model": "resnet50", "test": "<node id>", "source": "p150", "destination": "quasar-port",
 "port_implementation": "models/demos/vision/classification/resnet50/quasar", "profile_values": {},
 "tt_metal_ref": "origin/main", "tt_metal_commit": "<40-char sha>", "date": "YYYY-MM-DD", "drive_url": null,
 "entry_test_passes": {}, "port_e2e": null}
```

`entry_test_passes`, per p100/p150 target: where the entry test is shown to pass, either
`{"ci": "<workflow file>: <job>"}` or `{"by": "...", "date": "...", "result": "<pcc line>"}` for a
run the user reports. It backs the `✅` rule for p100/p150 in `target-status.md`.

`port_e2e`, when the user reports a passing Quasar-port e2e run: `{"by": "...", "date": "...",
"config": "ttsim <version>, <grid>, batch <n>, <dtype>", "result": "<pcc/top-1 line>"}`.

Measured phases add: `measured_ops` (path of `measured_ops.csv`, set by `merge_measured.py`),
`ops_csv` (the raw Tracy report), `window` (`warm` or `cold`), and for the destination phase
`ttsim_checkout`, `ttsim_commit`, `simulator_library`, `simulator_arch` and `ttsim_environment`
(the exact non-secret variables `kernel_tests.py run` sets).

Earlier runs are never an input to the analysis: during inventory, tracing and the Quasar pass,
read no file of an earlier run under `op-analysis/` (the current run's temporary test is the
only file there you read). Only the scripts open an earlier run: `compare_runs.py` in the
merge step after the validator prints `OK`.

For a new run, if `op-analysis/<model>/static/` already has a run, ask once:

- **overwrite**: delete that run directory and write the new one; if it has a `drive_url`,
  update the same spreadsheet.
- **new**: new directory; the old one is untouched.
- **merge**: new directory, then for each table below present in both runs
  `python3 <plugin-root>/scripts/compare_runs.py --old <old>/<t>.csv --new <new>/<t>.csv --out <new>/changes_<t>.csv --key <key>`,
  and a "Changes since <old run>" section in `summary.md` listing the commit range and the
  counts per change type. On Drive, add a `Changes` tab to the existing spreadsheet.

  | Table | `--key` |
  |---|---|
  | `op_table.csv` | default (`stage,ttnn_api,device_op,program_factory`) |
  | `call_trace.csv` | `profile,stage` |
  | `port_blockers.csv` | `blocker` |
  | `host_ops.csv` | `host_work` |
  | `host_fallback.csv` | `scope` |
  | `footprint.csv` | `op_code` |
  | `kernel_ops.csv` | `device_op,program_factory,op_code` |
  | `kernel_test_results.csv` | `node_id` |

`measured_ops.csv` is not compared: per-op timings change every run, and the
  measured comparison is regenerated by `kernel_ops.py`. `compare_runs.py` exits 2 if a key column is missing.

### Interpreting a cross-run comparison

`compare_runs.py` is a syntactic table diff, not a semantic verdict. Before describing a change:

1. Check commit ancestry and dates. State when the nominally new run pins an older, stale or
   divergent ref.
2. Compare blob IDs for the model and entry test, then inspect whether the dispatch/factory sources
   cited by changed rows differ between SHAs.
3. Normalize representation changes: zero-launch rows versus notes, split versus merged identical
   variants, stage naming, `prim::` versus `ttnn::prim::`, and explanatory text embedded in key
   columns. Do not report these as model changes.
4. Compare launch totals and execution order before support-status counts. Reconcile status counts
   on the same set of unique launching variants.
5. For each remaining status/confidence disagreement, reopen pinned evidence from both commits and
   decide whether it is a source change or an analysis-quality difference. State which result is
   better supported; do not average the two reports.

Never use an earlier report as dispatch evidence for a new run. It is only a list of claims to
re-check against pinned source.
