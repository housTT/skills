# Measured source phase

Adds what the source device actually launches to `op_table.csv`, using the run's entry test on the
run's commit. This is the `source:tracy` measurement item, for a Blackhole or Wormhole source.

A Quasar source has no Tracy step: its source item is `source:ttsim-e2e`, the entry test on
TTsim. Prepare TTsim as in measured-destination.md, step 1, then
`python3 <plugin-root>/scripts/kernel_tests.py plan <run dir> --target quasar`,
`python3 <plugin-root>/scripts/kernel_tests.py check <run dir> --target quasar --tt-metal <checkout>`
(measured-destination.md, step 3) and
`python3 <plugin-root>/scripts/kernel_tests.py run <run dir> --target quasar --entry --tt-metal <checkout>`.
Its result is recorded in `kernel_test_results_quasar.csv` and reported in `summary.md`; it does
not set row statuses.

1. Run `python3 <plugin-root>/scripts/same_commit.py <run dir> --tt-metal <checkout>`. On failure,
   offer: check out the recorded commit and rebuild, or start a new static run. Never measure on
   another commit.
2. Probe the device: `python3 <plugin-root>/scripts/probe_device.py` through `tt-device-mcp`. The
   probed architecture and grid must equal the run's source profile in `run.json`; if the broker is
   unavailable, show the command and ask the user to run it in the built tt-metal environment.
3. If the entry test runs in a CI workflow, record its `pytest_timeout` in `run.json` as
   `ci_pytest_timeout_s`; `measurements.py` shows it as the item's upper bound. Run the entry test
   from `run.json` (`test`) under Tracy through `tt-device-mcp`:
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
   `grid_dependency` `not traced`, and no launch count on any destination.
   When the entry test passed, add `--entry-pass "<pass line>"` (for example `PCC 0.990856`) and
   `--date <YYYY-MM-DD>` of the run. The pass is then a recorded run (`target-status.md`): the
   script adds it to `run.json` `entry_test_passes` and decides every source `⚠️` row again, since
   that `⚠️` only said no passing run was found. A row the test launched becomes `✅`; a
   conditional row whose op code ran fewer times than its static rows allow stays `⚠️` (Tracy does
   not say which call site ran); a row it never launched stays `⚠️`. Each gets an evidence text
   saying that no CI workflow runs the entry test on this profile and that it passed on a local
   device under Tracy, with the pass line and date. `✅`, `❌` and `n/a` rows keep their status; a
   `❌` row the test launched is printed as a warning to resolve by hand.
6. For each `measured` row, fill every destination's status per `target-status.md` (kernel-op
   lookup) and, for Quasar targets, its `<t>:tests` column, then rerun `validate_static.py`.
7. `python3 <plugin-root>/scripts/kernel_ops.py <run dir> --measured <run dir>/measured_ops.csv`
   shows static and measured launches per op code side by side. Explain every difference in
   `summary.md` (conditional moves, runtime reroutes, ops the static trace missed). Do not change
   static rows to match the measurement.
8. After `--entry-pass`, rewrite every sentence in `summary.md` (and any port-blocker or host-op
   text) that still says no passing source run exists or that the entry test is unverified on the
   source: state that no CI workflow runs it on this profile and that it passed on a local device
   under Tracy, with the pass line, and recount the source statuses from `kernel_ops.py`. Rerun
   `validate_static.py`; it warns about measured, unconditional source rows still `⚠️` while
   `entry_test_passes` is set.

When Tracy cannot run or the test fails (no device, profiler build missing, test failure), record
it with `python3 <plugin-root>/scripts/measurements.py mark <run dir> source:tracy failed --note "<reason>"`
so the next session shows why. A successful merge marks the item done by itself.

Silicon numbers come from the scripts, never from reading a CSV by eye. Do not claim a host
percentage or footprint the scripts did not produce.
