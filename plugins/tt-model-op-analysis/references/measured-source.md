# Measured source phase

Adds what the source device actually launches to `op_table.csv`, using the static run's entry test
on the static run's commit.

1. Run `python3 <plugin-root>/scripts/same_commit.py <run dir> --tt-metal <checkout>`. On failure,
   offer: check out the recorded commit and rebuild, or start a new static run. Never measure on
   another commit.
2. Probe the device: `python3 <plugin-root>/scripts/probe_device.py` through `tt-device-mcp`. The
   probed architecture and grid must equal the run's source profile in `run.json`; if the broker is
   unavailable, show the command and ask the user to run it in the built tt-metal environment.
3. Run the entry test from `run.json` (`test`) under Tracy through `tt-device-mcp`:
   `python -m tracy -p -r -v -m "pytest <node id>"`. If the test has `op-analysis-start` /
   `op-analysis-end` signposts the window is warm; otherwise it is cold and labelled so (host times
   then include compilation). Do not combine watcher or DPRINT with profiling. Revert any local
   test edit afterwards and say so.
4. Use the newest `ops_perf_results_*.csv` under `$TT_METAL_HOME/generated/profiler/reports/`:
   `python3 <plugin-root>/scripts/tracy_report.py <csv> <run dir> [--start op-analysis-start --end op-analysis-end]`
   writes `measured_ops.csv`, `host_fallback.csv` and `footprint.csv`. Pass `--peak-dram-mb` only
   with a measured value. Record the raw CSV path in `run.json` (`ops_csv`).
5. `python3 <plugin-root>/scripts/merge_measured.py <run dir> <run dir>/measured_ops.csv --inferences <n>`,
   where `<n>` is the number of forward passes in the measured window (1 for a warm signposted
   window; a cold window over the whole test counts every forward the test runs). Static rows seen
   in Tracy become `static+measured`; op codes only Tracy shows become `measured` rows after the
   static row that ran just before them, with launches per inference, `call_site` and
   `grid_dependency` `not traced`, and no launch count on a destination profile.
6. For each `measured` row, fill the destination status per `target-status.md` (kernel-op lookup)
   and its `<t>:tests` column, then rerun `validate_static.py`.
7. `python3 <plugin-root>/scripts/kernel_ops.py <run dir> --measured <run dir>/measured_ops.csv`
   shows static and measured launches per op code side by side. Explain every difference in
   `summary.md` (conditional moves, runtime reroutes, ops the static trace missed). Do not change
   static rows to match the measurement.

Silicon numbers come from the scripts, never from reading a CSV by eye. Do not claim a host
percentage or footprint the scripts did not produce.
