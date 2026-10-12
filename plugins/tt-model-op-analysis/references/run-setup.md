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

1. Ask which model to analyse. Do no model-specific discovery before the answer. Then ask for the
   output root (or state the configured one) and list the model's runs with
   `python3 <plugin-root>/scripts/runs.py list <root> <model>`: implementation, source profile,
   commit and date, static state, destinations and pending measurement items. Ask: continue one of
   them, or start a new run. A run in an older format (a `run.json` with `targets` or a single
   `destination`, plugin 0.3.0 or earlier) cannot be extended; say so and offer a new run.
2. Preconditions, before any question that depends on them: probe a local device of each source
   architecture the user may pick (`probe_device.py` through `tt-device-mcp`) and TTsim readiness
   for Quasar (`ttsim.md`). Show what is available; measurement items without their hardware stay
   pending.
3. New run, ref: ask which ref to pin. Recommend the local checkout's HEAD when a matching device
   or TTsim is available and the user is likely to measure (measurements must run on the run's
   commit, and a checkout at HEAD needs no switch or rebuild); otherwise recommend a freshly
   fetched `origin/main`. Say which and why. Resolve the ref before any discovery so every path,
   skip marker and CI claim comes from the pinned SHA.
4. Ask for the source profile from `<plugin-root>/references/targets.md`: where the implementation
   runs today. Show each option's one-line question.
5. Discover implementations compatible with the source profile and show the table from section 1.
   Ask which implementation to analyse.
6. Rank only the whole-model test candidates for that implementation and source. Recommend the
   best candidate with a concrete reason, then ask which test to use. Measurements rerun this same
   test.
7. If a run with this identity (model, implementation, entry test, source, commit) already exists,
   say so and continue it; never trace it again.

Destinations are asked after static analysis (static-phase.md, "Adding a destination"), never
during setup.

Do not inspect model source, create output or delegate tracing until the applicable setup is
complete. Candidate discovery may inspect directory structure, tests, skip markers and CI workflow
references at the pinned SHA, but not trace the model implementation yet.

## 1. Implementation and test

Ask which model. In the tt-metal checkout, find:

- every model directory (`git ls-tree -r --name-only <sha> models/ | grep -i <model>`),
- every implementation module (common, functional, interleaved, sharded, per-arch
  `blackhole/`, `wormhole/`, `t3000/`, `quasar/`),
- for each implementation, its whole-model test candidates (below), with the workflow under
  `.github/workflows/` that runs each one, if any,
- the skip markers on each of them: unconditional `skip` and every arch `skipif`
  (`is_blackhole`, `is_wormhole_b0`, ...), with their reasons.

A whole-model test runs the model forward from the model input through every block to the head
output. Confirm it by reading the test body: it calls the model's top-level forward (directly or
through a test-infra `run()`), not one layer, block or stage. Component tests
(`test_<model>_attention`, `_layer`, `_encoder`, `_embeddings`, ...) are never candidates.

Candidates: end-to-end PCC tests and demo tests, plus perf tests (device-perf: the
`command = f"pytest ..."` string in `*perf_device*.py` or `*device_perf*.py`; e2e perf such as
`test_*perf_e2e*.py`) whose test body runs the whole model, so one test serves static analysis and
measurements. For a Quasar source, candidates are whole-model tests of that implementation that run
on TTsim; no CI requirement.

Evaluate the implementation and entry test against the source profile only. Do not offer
incompatible implementations as choices; summarize how many were excluded and make their details
available on request.

Show one row per compatible implementation: module path, target fit, candidate node ids, skip
markers and whether CI runs them on the relevant architecture. Never derive the implementation
from a proposed test. Recommend an architecture-specific implementation exercised by CI or a demo,
not a reference or functional version. If several remain, prefer full target coverage and runnable
tests over naming or directory proximity.

After the user picks an implementation, rank its tests in this order:

1. runnable on the source target;
2. explicitly exercised by CI on the relevant architecture;
3. a warm-capable whole-model perf or PCC test, then a whole-model PCC/demo;
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

When a model has a Blackhole and a Quasar implementation (for example
`models/demos/vision/classification/resnet50/ttnn_resnet` and `.../resnet50/quasar`), each is its
own run with its own source. Comparing the two is a separate step on two runs.

## 2. Pin the source

Record the full SHA of the commit analysed (`git rev-parse <ref>`; the recommendation is in the
setup order, step 3). Read every file as `git show <sha>:<path>`; never read
the working tree, except an approved temporary test (section 1). For Quasar, record the ttsim
version if the user knows it.

zsh trap: `$VAR:path` applies the `:t`/`:h` modifiers. Write `<sha>:path` with the literal SHA or
use `"${VAR}:path"`.

Treat `main`, `origin/main` and `HEAD` as different refs; never silently substitute one for another.
When the user says `main`, resolve local `main` exactly, and report whether an already-present
`origin/main` points elsewhere and which is newer by ancestry. Fetch only when the user selected a
fresh remote ref and the environment permits it. Record both the symbolic ref and resolved SHA in
the run notes so a later comparison can detect a stale or divergent pin.

## 3. Output location and run identity

Root: current working directory unless the user names one; never inside the tt-metal checkout.

- Run: `<root>/op-analysis/<model>/<impl-slug>/<source>/<sha7>/`. `impl-slug` is the
  implementation module's directory name, or its parent's name when that directory is `tt`
  (`ttnn_resnet`, `quasar`). One directory holds the source trace, every destination and every
  measurement of that run.
- Identity: model, implementation module, entry test, source profile and `tt_metal_commit`. The
  same model, implementation and source on another commit is a new run. There is no overwrite or
  merge: a run with the same identity is continued.

`run.json` fields:

```json
{"kind": "static", "model": "resnet50", "implementation": "<module path>", "test": "<node id>",
 "source": "p150", "destinations": ["quasar-bf16"], "stage_blocks": ["stem", "layer1", "head"],
 "profile_values": {}, "tt_metal_ref": "origin/main", "tt_metal_commit": "<40-char sha>",
 "date": "YYYY-MM-DD", "drive_url": null, "entry_test_passes": {}}
```

`entry_test_passes`, for a Blackhole or Wormhole source: where the entry test is shown to pass,
either `{"ci": "<workflow file>: <job>"}` or `{"by": "...", "date": "...", "result": "<pcc line>"}`
for a run the user reports. It backs the `✅` rule in `target-status.md`.

Measurements add: `measured_ops` (path of `measured_ops.csv`, set by `merge_measured.py`),
`measured_inferences`, `ops_csv` (the raw Tracy report), `window` (`warm` or `cold`),
`ci_pytest_timeout_s` (the CI `pytest_timeout` of the entry test, when it has one), `measurements`
(marks from `measurements.py mark`), and for TTsim `ttsim_checkout`, `ttsim_commit`,
`simulator_library`, `simulator_arch` and `ttsim_environment` (the exact non-secret variables
`kernel_tests.py run` sets).

Other runs are never evidence: during inventory, tracing and status decisions, read no file of
another run under `op-analysis/`. The run being continued is different:
the run being continued is the input for adding a destination or a measurement, and its tables are
read and extended. The scripts (`runs.py`, `compare_runs.py`) may open any run.

On request, two runs can be compared table by table:
`python3 <plugin-root>/scripts/compare_runs.py --old <old>/<t>.csv --new <new>/<t>.csv --out <new>/changes_<t>.csv --key <key>`.

| Table | `--key` |
|---|---|
| `op_table.csv` | default (`stage,ttnn_api,device_op,program_factory`) |
| `call_trace.csv` | `profile,stage` |
| `port_blockers.csv` | `blocker` |
| `host_ops.csv` | `host_work` |
| `host_fallback.csv` | `scope` |
| `footprint.csv` | `op_code` |
| `kernel_ops.csv` | `device_op,program_factory,op_code` |
| `kernel_test_results_<target>.csv` | `node_id` |

`measured_ops.csv` is not compared: per-op timings change every run. `compare_runs.py` exits 2 if a
key column is missing.

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
