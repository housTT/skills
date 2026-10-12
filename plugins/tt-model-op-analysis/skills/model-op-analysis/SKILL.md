---
name: model-op-analysis
description: Analyse the kernel ops a tt-metal model test runs and whether they port to a destination architecture. Traces every ttnn call to its device op and program factory at a pinned commit, compares one source target (P100 or P150) with one destination (P100, P150, Quasar unchanged, an existing Quasar port or a Quasar BF16 projection), and optionally adds measured data - a Tracy run of the same entry test on the source device and TTsim runs of the destination's kernel-op tests. Use for "op table", "which kernel ops does model X use", "can model X run on Quasar", or kernel-op support and porting analysis.
---

# Model op analysis

Analyses a model's kernel ops and how they port to a destination architecture. Three modes:

- **static**: read source at a pinned commit and build validated tables; no device.
- **static + measured**: static, then measured data on top of it.
- **measured over an existing static run**: add measured data to a run made earlier, on the same
  commit.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this SKILL.md;
resolve every `<plugin-root>/...` path against it.

## Phases

1. **Setup.** Follow `<plugin-root>/references/run-setup.md`: the mode, then for a new run the
   model, pinned ref, source and destination targets, implementation, entry test and output
   directory, or for an existing run the run to extend. Then the preconditions of the measured
   phases the user wants: a local source device (probe through `tt-device-mcp`) and a ready TTsim
   build for the destination (`<plugin-root>/references/ttsim.md`). Report missing preconditions
   before any analysis starts; the user may drop a measured phase or stop.
2. **Static** (new runs). Follow `<plugin-root>/references/static-phase.md`. It ends when
   `python3 <plugin-root>/scripts/validate_static.py <run dir>` prints `OK`.
3. **Measured source** (if chosen). Follow `<plugin-root>/references/measured-source.md`:
   `python3 <plugin-root>/scripts/same_commit.py <run dir> --tt-metal <checkout>` must pass, then a
   Tracy run of the run's entry test on the source device, then
   `python3 <plugin-root>/scripts/merge_measured.py <run dir> <measured_ops.csv>`.
4. **Measured destination** (if chosen). Follow `<plugin-root>/references/measured-destination.md`:
   find kernel-op tests (including new ones for `⚠️` rows), confirm the list and time budget with
   the user, then `python3 <plugin-root>/scripts/kernel_tests.py` `plan`, `run` and `apply`.
5. **Report.** Run `python3 <plugin-root>/scripts/kernel_ops.py <run dir>` (with
   `--measured <measured_ops.csv>` after phase 3), rerun the validator, and write `summary.md`
   with the per-kernel-op table first (static-phase.md, step 7). Ask about upload; if yes, follow
   `<plugin-root>/references/drive-upload.md`.

A run made before the `origin` column existed is upgraded first:
`python3 <plugin-root>/scripts/upgrade_run.py <run dir>`.

## Rules

- Ask one setup decision per turn; never infer choices from open files, IDE state or earlier runs.
- Read tt-metal sources as `git show <sha>:<path>`; never the working tree, except an approved
  temporary test (run-setup.md).
- Measured phases run on the run's `tt_metal_commit` only; `same_commit.py` is the gate.
- Physical-device commands go through `tt-device-mcp`; TTsim runs on the host (`ttsim.md`).
- Subagents follow the contract in `<plugin-root>/references/dispatch-tracing.md`: no nested
  subagents, one complete result.
- Write CSVs with the scripts or a heredoc into the run directory; never retype a table to fix it.
- Do not edit the tt-metal checkout, except an approved temporary test (run-setup.md).
- Output stands alone: a reader who knows no issue, ticket, tracker or earlier conversation must
  understand every file. Never refer to criteria, items or numbers defined elsewhere; state what
  is meant. A link may cite a source, never replace a definition.
- Report what the validator checked, what stayed `unverified`, and which statuses rest on an
  actual TTsim run versus test markers.
