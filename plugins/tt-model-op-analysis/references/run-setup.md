# Run setup (both skills)

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

### Static setup order

1. Ask which model to analyse. Do no model-specific discovery before the answer.
2. Ask for the architecture scope: Blackhole, Quasar, or both. If Blackhole is included, ask next
   for P100, P150, or both. Architecture/profile selection must finish before implementation or
   test candidates are presented.
3. Ask which source ref to pin. A suggested ref is allowed, but it is not confirmation. Resolve it
   before candidate discovery so every path, skip marker and CI claim comes from the pinned SHA.
4. Discover implementations compatible with the selected as-written profiles and show the table
   from section 1. Ask which implementation to analyse.
5. Rank only the whole-model test candidates for that implementation and the selected as-written
   profiles. Recommend the best candidate with a concrete reason, then ask which test to use.
6. Ask for the output root, explain it cannot be inside the tt-metal checkout, and stop.

### Measured setup order

1. Before asking about the model, perform backend discovery from
   `<plugin-root>/skills/measured-op-analysis/SKILL.md`. Ask for silicon, TTsim, or both using only
   discovered available/buildable choices.
2. If silicon is included, probe its actual architecture and grids. If TTsim is included, ask for
   one of the architectures reported by its checkout and complete its readiness check. Do not offer
   an architecture unsupported by the selected backend.
3. Ask which model to analyse.
4. Ask which source ref represents the checkout/build that will run. Resolve it before candidate
   discovery.
5. For each selected backend, discover and show only compatible implementations, then ask which one
   to analyse. Share a choice only when compatibility is proven for both.
6. Rank runnable whole-model tests separately per backend. Recommend the best candidate with a
   concrete reason, then ask which test to use. Different architectures may require different tests.
7. Ask for the output root once, explain it cannot be inside the tt-metal checkout, and stop.

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
- **measured silicon**: the static candidates, plus perf tests (device-perf: the `command = f"pytest ..."`
  string in `*perf_device*.py` or `*device_perf*.py`; e2e perf such as
  `test_*perf_e2e*.py`) whose test body runs the whole model.
- **measured TTsim**: simulator-specific or ordinary end-to-end PCC/demo tests that run the whole
  model on the selected simulated architecture. Do not recommend a perf test merely because it is
  named `perf`; no performance is collected from TTsim.

For static analysis, evaluate the primary implementation and entry test against every selected
as-written profile. When Blackhole and Quasar are both selected, the chosen Blackhole
implementation/test is primary; discover a Quasar implementation separately as port evidence per
the rule below. Do not require the Blackhole entry test to run on Quasar. For measured analysis,
evaluate compatibility separately against each selected backend profile. Do not offer incompatible
primary implementations as choices; summarize how many were excluded and make their details
available on request. If no single primary implementation supports all selected P100/P150 profiles, do not
silently combine implementations: ask whether to create separate runs or narrow the target set.

Show one row per compatible implementation: module path, target fit, candidate node ids, skip
markers and whether CI runs them on the relevant architecture. Never derive the implementation
from a proposed test. Recommend an architecture-specific implementation exercised by CI or a demo,
not a reference or functional version. If several remain, prefer full target coverage and runnable
tests over naming or directory proximity.

After the user picks an implementation, rank its tests in this order:

1. runnable on every selected static as-written profile, or on the measured backend being configured;
2. explicitly exercised by CI on the relevant architecture;
3. a whole-model PCC/demo for static or TTsim, or a warm-capable perf/PCC test for measured silicon;
4. the least setup indirection and fewest unrelated parametrizations.

Present the top candidates with their node ids, relevant parameters, skip status, CI coverage and a
one-line recommendation reason. For TTsim also show the checkout/build target, interpreter, working
directory and test-specific environment required by pinned source or a launch configuration the
user supplied. Treat such a launch configuration as candidate evidence, not as a global default.
Never recommend a test skipped unconditionally or for a relevant architecture. If none is runnable,
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
- **measured**: as an untracked file next to the model's tests in the tt-metal checkout, so its
  conftest and fixtures apply. Write it warm from the start (two forwards with the signposts
  from the measured workflow's warm-run step). Copy it into the run directory, and delete it
  from the checkout after the run and say so.

Record it in `run.json` as `"test": "<run dir>/temp_test_<model>.py::<test name>"` and
`"temporary_test": true`, and state in `summary.md` that the entry test is temporary and not part
of tt-metal.

If a Blackhole and a Quasar implementation both exist (for example
`models/demos/vision/classification/resnet50/ttnn_resnet` and `.../resnet50/quasar`), the
Quasar port goes in the `quasar:port` column, never mixed into the Blackhole rows.

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

- Static run: `<root>/op-analysis/<model>/static/<YYYY-MM-DD>_<sha7>/`.
- Measured run: `<root>/op-analysis/<model>/measured/<YYYY-MM-DD>_<sha7>_<backend>-<profile>/`.

Use `silicon` or `ttsim` for `<backend>`. If both are selected, create sibling measured runs with
independent `run.json`, logs and summaries; never merge their artifacts.

`run.json` fields:

```json
{"kind": "static", "model": "resnet50", "test": "<node id>", "targets": ["p150", "quasar"],
 "tt_metal_ref": "origin/main", "tt_metal_commit": "<40-char sha>", "date": "YYYY-MM-DD", "drive_url": null,
 "port_e2e": null}
```

`port_e2e`, when the user reports a passing Quasar-port e2e run: `{"by": "...", "date": "...",
"config": "ttsim <version>, <grid>, batch <n>, <dtype>", "result": "<pcc/top-1 line>"}`.

Measured `run.json` additionally records `backend` (`silicon` or `ttsim`), `profile`, and
`observation_scope` (`silicon-profiled` or `ttsim-operations`). A TTsim run also records
`ttsim_checkout`, `ttsim_commit`, `simulator_library`, `simulator_arch`, `graph_database` and the
exact non-secret `environment` used.

Earlier runs are never an input to the analysis: during inventory, tracing and the Quasar pass,
read no file of an earlier run under `op-analysis/` (the current run's temporary test is the
only file there you read). Only the scripts open an earlier run: `compare_runs.py` in the
merge step after the validator prints `OK`, and `diff_reports.py` in the measured diff step.

If `op-analysis/<model>/<kind>/` already has a run, ask once:

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
  | `quasar_blockers.csv` | `blocker` |
  | `host_ops.csv` | `host_work` |
  | `host_fallback.csv` | `scope` |
  | `footprint.csv` | `op_code` |
  | `simulated_ops.csv` | `rank,operation_id` |
  | `simulated_op_counts.csv` | `operation` |

`measured_ops.csv` and `diff.csv` are not compared: per-op timings change every run, and the
  diff is regenerated. `compare_runs.py` exits 2 if a key column is missing.

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
