# TTsim for Quasar measurements

Read this reference for the Quasar measurement items: kernel-op tests of a Quasar target and the entry test of a Quasar source, all on TTsim.

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
a build, rerun discovery and require `ready=true` before running any test.

If no checkout is found, do not present TTsim as currently available. Say that the user may provide
its path; do not clone or build it without a separate request.

When `--tt-metal-home` is supplied, discovery also prints a `recommended_run` object for each
architecture. Use its working directory, interpreter and environment as the runnable baseline;
do not reconstruct these paths by hand. A checked-in `.vscode/launch.json`, test docstring or task
that targets the same simulator build may add model/test-specific variables to a separate batch.

## Environment

Construct a fresh environment and record it in `run.json` as `ttsim_environment`;
`kernel_tests.py run` applies it to every test. Do not inherit stale
silicon, watcher, profiler or another-architecture simulator variables. The common fields are:

```text
TT_METAL_HOME=<tt-metal checkout>
TT_METAL_SIMULATOR=<ready build_dir>/libttsim.so
TT_METAL_SIMULATOR_HOME=<ready build_dir>
TT_METAL_SLOW_DISPATCH_MODE=1
ARCH_NAME=<selected architecture>
CHIP_ARCH=<selected architecture>
```

Run from the tt-metal checkout. Prefer its `python_env/bin/python` when present and consistent with
the build; otherwise use the interpreter already proven to import that checkout's `ttnn`. Record the
interpreter and working directory.

Use `wormhole_b0`, `blackhole`, `quasar` or `trizor` as reported by discovery. For Quasar, inspect
the pinned `scripts/run_safe_pytest.sh` and the kernel-op tests for the required NoC API setting; set
`TT_METAL_QUASAR_NOC_API_VERSION=1` when that pinned source says TTsim does not implement the
default API.

Test-specific variables are evidence, not global defaults. Inspect the tests, their conftest,
documented invocation and an applicable checked-in launch/task configuration. Add variables such as
PCC logging, forced program routes or DPRINT only when the tests require them; when tests need
different variables, run them as separate `kernel_tests.py run --only ... --env-json ...` batches. In particular,
never apply `TT_METAL_CORE_GRID_OVERRIDE_TODEPRECATE` to every Quasar test: some require the full
grid. When an override is required, record both its inclusive end coordinate and the resulting grid.

TTsim opens a local Unix socket. If a managed sandbox reports `Failed to bind simulation server
socket ... Operation not permitted`, rerun the same command with the required outside-sandbox
approval; do not classify that setup error as a kernel failure.

Run TTsim discovery, build and pytest directly on the host, not through the physical-device broker.
Do not run two TTsim jobs against the same build directory concurrently unless the simulator
documentation explicitly permits it.

## Kernel-op tests

The measured destination phase (`<plugin-root>/references/measured-destination.md`) runs one
kernel-op test per process with `kernel_tests.py`. TTsim proves execution and correctness on the
simulated architecture, never performance: do not report TTsim timing, throughput, host fallback
or footprint.

Before the first measured run, confirm parameterized node IDs with pytest collection when they were
copied or generated rather than obtained from a current collection. A `not found` collection error
means the plan's node ID must be corrected and regenerated; it is not a kernel result.
