# Measured destination phase

Runs the destination's kernel-op tests on TTsim and replaces assumed results with run results.
Only for Quasar destinations (`quasar`, `quasar-port`, `quasar-bf16`).
Do not run the port's whole-model test; this phase tests kernel ops one by one.

1. Readiness: `python3 <plugin-root>/scripts/same_commit.py <run dir> --tt-metal <checkout>` must
   pass (`kernel_tests.py run` checks it again). Follow `<plugin-root>/references/ttsim.md`
   discovery and readiness for the destination architecture, and record `ttsim_environment` (the
   exact non-secret variables) in `run.json`.
2. Kernel-op tests: every row's `<dest>:tests` lists the tests that decide its status. For each
   `⚠️` row with no test, search the Quasar tests (the model's Quasar op tests,
   `tests/ttnn/**/quasar/`, `tests/scripts/quasar/*.yaml`) for one that calls the same kernel op
   (device op, program factory, layout, dtype); add its node id and say so in the evidence. Do not
   write new tests.
3. `python3 <plugin-root>/scripts/kernel_tests.py plan <run dir>` lists the unique node ids. Show
   the count and a time estimate (default timeout 900 s per test) and stop for the user's approval.
4. `python3 <plugin-root>/scripts/kernel_tests.py run <run dir> --tt-metal <checkout> --pytest-cmd "<interpreter> -m pytest"`
   runs each node with plain pytest on the host under `ttsim_environment`, one process per node; a
   timeout is recorded as `hang`. Without `--pytest-cmd` it uses `<checkout>/python_env/bin/python`
   when present; the command used is recorded in `run.json`. It resumes after an interruption;
   `--rerun` repeats finished nodes. Tests that need different variables run as separate batches:
   `--only <text>` selects plan nodes, `--env-json <file>` gives that batch's environment, and
   earlier batches' results are kept.
5. Read the logs of every `error` result before applying: an error means the test could not run
   (environment, import, collection), not that the kernel op fails. Fix the environment and rerun
   those nodes.
6. `python3 <plugin-root>/scripts/kernel_tests.py apply <run dir>` sets `<dest>:test_result` and the
   status per row: a `fail`, `hang` or `xfail` of any of its tests gives `❌`; `✅` needs every test
   of the row to pass; `error`, `skip` and unrun tests leave the status unchanged. The evidence
   starts with `TTsim run: <node> <result>`.
7. Rerun `validate_static.py` and `kernel_ops.py`. In `summary.md`, state which statuses rest on a
   TTsim run and which on test markers only.

TTsim proves execution and correctness on the simulated architecture, never performance.
