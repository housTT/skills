---
name: measured-op-analysis
description: Observe the operations a tt-metal model actually executes on accessible silicon and/or TTsim. On silicon, collect Tracy timing, host fallback and footprint; on TTsim, collect execution-order TTNN operations and correctness without performance claims. Use for measured ops, host fallback, device footprint, or simulator operation validation.
---

# Measured op analysis

Needs a built tt-metal checkout and accessible Tenstorrent silicon and/or a local TTsim checkout.
Silicon numbers come from `<plugin-root>/scripts/tracy_report.py`, never from reading a CSV by eye.
TTsim is operation-observation only and is never presented as silicon performance.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this
SKILL.md; resolve every `<plugin-root>/...` path against it.

## Steps

1. **Backend discovery and choice.** Before asking for a model, check physical-device availability
   through `tt-device-mcp` and read `<plugin-root>/references/ttsim.md` to discover local TTsim
   checkouts. Ask whether to analyse on silicon, TTsim, or both, showing only available or buildable
   choices. A TTsim checkout with a missing build is `buildable`, not ready; say that before the
   choice. If neither backend is usable, report the prerequisites and offer static analysis.
2. **Backend preflight.** For silicon, run
   `python3 <plugin-root>/scripts/probe_device.py` through `tt-device-mcp`; serialize it with every
   other device command. If the broker is unavailable, show the command and ask the user to run it
   in the built tt-metal environment. For TTsim, ask which discovered architecture to use, then
   follow `ttsim.md`; ask before building a missing library. Never guess from the IDE, host name,
   launch history or an earlier run. When both are selected, configure independent backend runs.
3. **Setup.** Follow the measured order in `<plugin-root>/references/run-setup.md`. Backend profiles
   are fixed by the probes: do not offer unrelated architectures. Select compatible implementations
   and whole-model tests separately per backend; the model and pinned source ref remain shared.
   Prefer the same entry test as a static run only when compatible. Record actual arch and grids for
   silicon. For TTsim record its checkout SHA, library, selected architecture and exact non-secret
   environment in `run.json`.
4. **Prepare execution.** For silicon, check that the test runs the model at least twice. If not,
   offer an existing warm variant or a local uncommitted edit that runs it twice with Tracy
   signposts around the second run; without signposts the report is `cold`. For TTsim, one successful
   whole-model forward is sufficient unless the selected test itself requires more; enable the TTNN
   graph report exactly as described in `ttsim.md`.
5. **Run separately.** Silicon command:
   `python -m tracy -p -r -v -m "pytest <node id>"`; route it through `tt-device-mcp` when available,
   otherwise show it and ask the user to run it. TTsim command: ordinary `pytest <node id>` with its
   fresh simulator environment, run directly on the host—not Tracy and not the device broker. Never
   execute silicon and TTsim in one process. Do not combine watcher or DPRINT with silicon profiling;
   on TTsim use DPRINT only when the exact test requires it.
6. **Process silicon.** Use the newest `ops_perf_results_*.csv` under
   `$TT_METAL_HOME/generated/profiler/reports/`:
   `python3 <plugin-root>/scripts/tracy_report.py <csv> <run dir> --start op-analysis-start --end op-analysis-end`
   (omit flags for a cold run). Pass `--peak-dram-mb` only with a measured value.
7. **Process TTsim.** Find the exact graph-report `db.sqlite` under the configured report path and
   run `python3 <plugin-root>/scripts/ttsim_report.py <db.sqlite> <run dir>`. The required outputs are
   `simulated_ops.csv`, `simulated_op_counts.csv`, `ttsim_graph.sqlite`, the pytest log and
   correctness result. If capture produced no operation rows, report that failure; never substitute
   the static table.
8. **Diff.** Only for a warm silicon window: if a static run exists for the same profile, run
   `python3 <plugin-root>/scripts/diff_reports.py --static <static run>/op_table.csv --measured <run dir>/measured_ops.csv --profile <p> --out <run dir>/diff.csv`.
   Report differences; do not edit the static table. Do not pass TTsim output to this timing-oriented
   diff; compare its operation names/counts descriptively and keep architecture differences explicit.
9. **Summary.** Write one `summary.md` per backend. Silicon: model, test, SHA, device/profile, window,
   host fallback, top ops by device-kernel time, max cores and diff counts. TTsim: model, test, SHA,
   simulator checkout/SHA/library/architecture, grid and exact environment, correctness result,
   operation occurrence/unique counts and capture limitations; no timing, throughput or footprint.
10. **Upload.** Ask; if yes, follow `<plugin-root>/references/drive-upload.md`.

## Rules

- Revert any local test edit and delete any temporary test from the checkout after the run, and say
  so.
- A cold silicon window includes compilation in host times; label it, never present it as warm.
- Keep silicon and TTsim artifacts in separate run directories even when both were chosen.
- Keep the raw silicon ops CSV in `run.json` (`ops_csv`) and copy it into the silicon run directory.
- Do not claim a host percentage or footprint that the scripts did not produce.
- TTsim operation rows are observed TTNN graph operations, not proof of a particular low-level
  program factory. Do not silently equate them with static `device_op` rows.
- Output stands alone. `summary.md` ends with a “How to read this” section defining every emitted
  column and, for silicon, the window label and Tracy host/device op classes; for TTsim, define the
  graph-capture scope and explicitly state that no performance was measured.
