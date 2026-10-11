# TTsim backend for measured analysis

Read this reference only when measured analysis may use TTsim.

## Discovery and selection

Run:

```bash
python3 <plugin-root>/scripts/discover_ttsim.py --tt-metal-home <tt-metal checkout>
```

The script checks an explicit `--ttsim-home`, `TTSIM_HOME`, then `ttsim-private` and `ttsim`
siblings of the tt-metal checkout. A directory is a usable prerequisite only when it contains
`make.py` and `src/`. Do not infer TTsim availability from a launch configuration whose checkout no
longer exists.

Each reported architecture is either:

- **ready**: `libttsim.so` and the adjacent `soc_descriptor.yaml` exist;
- **buildable**: the checkout exists but the release library or descriptor is missing.

Offer only architectures reported by the selected checkout. If the selected architecture is merely
buildable, show the exact `build_target` and ask before running `./make.py <build_target>` in the
TTsim checkout. Building generated output is optional and is never implied by choosing TTsim. After
a build, rerun discovery and require `ready=true` before executing a model test.

If no checkout is found, do not present TTsim as currently available. Say that the user may provide
its path; do not clone or build it without a separate request.

## Environment

Construct a fresh environment for each TTsim run and record it in `run.json`. Do not inherit stale
silicon, watcher, profiler or another-architecture simulator variables. The common fields are:

```text
TT_METAL_HOME=<tt-metal checkout>
TT_METAL_SIMULATOR=<ready build_dir>/libttsim.so
TT_METAL_SIMULATOR_HOME=<ready build_dir>
TT_METAL_SLOW_DISPATCH_MODE=1
ARCH_NAME=<selected architecture>
CHIP_ARCH=<selected architecture>
TTNN_CONFIG_OVERRIDES={"enable_fast_runtime_mode":false,"enable_logging":true,"enable_graph_report":true,"root_report_path":"<run dir>/ttnn-reports","report_name":"ttsim-<architecture>-<model>"}
```

Run from the tt-metal checkout. Prefer its `python_env/bin/python` when present and consistent with
the build; otherwise use the interpreter already proven to import that checkout's `ttnn`. Record the
interpreter and working directory.

Use `wormhole_b0`, `blackhole`, `quasar` or `trizor` as reported by discovery. For Quasar, inspect
the pinned `scripts/run_safe_pytest.sh` and selected test for the required NoC API setting; set
`TT_METAL_QUASAR_NOC_API_VERSION=1` when that pinned source says TTsim does not implement the
default API.

Merge the graph-report keys above into an existing `TTNN_CONFIG_OVERRIDES` object rather than
discarding test-required keys. The graph report is the primary TTsim operation source; it records
operation identity and order without making a performance claim.

Test-specific variables are evidence, not global defaults. Inspect the selected test, its conftest,
documented invocation and an applicable checked-in launch/task configuration. Add variables such as
PCC logging, forced program routes or DPRINT only when that exact test requires them. In particular,
never apply `TT_METAL_CORE_GRID_OVERRIDE_TODEPRECATE` to every Quasar test: some require the full
grid. When an override is required, record both its inclusive end coordinate and the resulting grid.

Run TTsim discovery, build and pytest directly on the host, not through the physical-device broker.
Do not run two TTsim jobs against the same build directory concurrently unless the simulator
documentation explicitly permits it.

## TTsim operation observation

TTsim proves execution and correctness for the simulated architecture. It does not provide silicon
performance. Keep it in a separate run directory from every physical-device run.

Run ordinary pytest with TTNN graph reporting enabled; do not run TTsim under Tracy or any device
performance profiler. Locate the exact `db.sqlite` created below the configured
`root_report_path`, then run:

```bash
python3 <plugin-root>/scripts/ttsim_report.py <db.sqlite> <run dir>
```

This writes `simulated_ops.csv` in execution order and `simulated_op_counts.csv` grouped by TTNN
operation. Preserve `ttsim_graph.sqlite`, the pytest log, pass/fail, PCC or other correctness result,
and the exact environment. If no graph database or no operation rows are produced, mark operation
capture failed and do not infer the executed list from static source.

Never create TTsim timing, throughput, host-fallback, footprint or silicon-comparison claims. The
word `measured` for this backend means observed execution, not performance measurement.

When both silicon and TTsim are selected, run them sequentially as independent backend runs. They
may use different architecture-specific implementations and entry tests for the same model.
