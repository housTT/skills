---
name: model-op-analysis
description: Analyse the kernel ops one implementation of a tt-metal model runs on its architecture (P100, P150, N150, one chip of an N300, or Quasar) and how they would port to other architectures. Traces every ttnn call to its device op and program factory at a pinned commit, decides support per kernel op, optionally adds destinations (same code on another profile, or a Quasar BF16 projection) without re-tracing, and resumes measurements item by item - a Tracy run of the entry test on the source device and TTsim runs of Quasar kernel-op tests. Use for "op table", "which kernel ops does model X use", "can model X run on Quasar", or kernel-op support and porting analysis.
---

# Model op analysis

One run = one implementation of a model, its entry test, a source profile and a tt-metal commit,
in `<root>/op-analysis/<model>/<impl-slug>/<source>/<sha7>/`. Static analysis is done once per
run; destinations and measurements are added to it, in this session or a later one.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this SKILL.md;
resolve every `<plugin-root>/...` path against it.

## Flow

1. **Model and inventory.** Ask for the model, then run
   `python3 <plugin-root>/scripts/runs.py list <root> <model>` and show its runs: implementation,
   source, commit, static state, destinations, pending measurements. Ask: continue a run, or start
   a new one. A run in an older format (plugin 0.3.0 or earlier) cannot be extended; offer a new
   run.
2. **Preconditions, early.** Probe what measurements could use before asking anything that depends
   on them: a local device of each candidate source architecture (through `tt-device-mcp`) and
   TTsim readiness for Quasar (`<plugin-root>/references/ttsim.md`). Report what is available.
3. **New run.** Follow `<plugin-root>/references/run-setup.md`: ref, source profile,
   implementation, entry test, output root. If the same identity exists, say so and continue that
   run instead of tracing again.
4. **Static.** Follow `<plugin-root>/references/static-phase.md`; it ends when
   `python3 <plugin-root>/scripts/validate_static.py <run dir>` prints `OK`. Skipped for a run whose
   static analysis is done.
5. **Destinations.** Ask whether to add a destination; repeat while the user wants more. Each one is
   added to the same run from the existing source trace (static-phase.md, "Adding a destination").
6. **Measurements.** Run `python3 <plugin-root>/scripts/measurements.py list <run dir> --pending`,
   show only those items (never ones already done) with their state and estimate, and let the user
   pick one or more. Source items follow
   `<plugin-root>/references/measured-source.md`
   (`python3 <plugin-root>/scripts/same_commit.py` first, then Tracy and
   `python3 <plugin-root>/scripts/merge_measured.py`); Quasar test items follow
   `<plugin-root>/references/measured-destination.md` (`python3 <plugin-root>/scripts/kernel_tests.py`).
   Unpicked items stay pending for a later session.
7. **Report.** `python3 <plugin-root>/scripts/kernel_ops.py <run dir>`, the validator again, and
   `summary.md` with the per-kernel-op table first. Ask about upload; if yes, follow
   `<plugin-root>/references/drive-upload.md`.

## Rules

- Ask one decision per turn, recommended option first; never infer choices from open files, IDE
  state or earlier runs.
- Read tt-metal sources as `git show <sha>:<path>`; never the working tree, except an approved
  temporary test (run-setup.md).
- Measurements run on the run's `tt_metal_commit` only; `same_commit.py` is the gate.
- Physical-device commands go through `tt-device-mcp`; TTsim runs on the host (`ttsim.md`).
- Subagents follow the contract in `<plugin-root>/references/dispatch-tracing.md`: no nested
  subagents, one complete result.
- Write CSVs with the scripts or a heredoc into the run directory; never retype a table to fix it.
- Do not edit the tt-metal checkout, except an approved temporary test (run-setup.md).
- Output stands alone: a reader who knows no issue, ticket, tracker or earlier conversation must
  understand every file. Never refer to criteria, items or numbers defined elsewhere; state what
  is meant. A link may cite a source, never replace a definition.
- Report what the validator checked, what stayed `unverified`, and which statuses rest on a TTsim
  run versus test markers.
