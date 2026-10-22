# TTsim for Quasar measurements

Read this reference for the Quasar measurement items: kernel-op tests of a Quasar target on TTsim, one node at a time. An entry test (a whole-model test) never runs on TTsim.

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

Use `wormhole_b0`, `blackhole`, `quasar` or `trizor` as reported by discovery. Simulator
workarounds change between tt-metal commits, so take them from the pinned
`scripts/run_safe_pytest.sh` (`git show <sha>:scripts/run_safe_pytest.sh`), never from memory or an
earlier run: add each variable it exports for TTsim (for example `TT_METAL_DISABLE_SFPLOADMACRO`),
and for Quasar set `TT_METAL_QUASAR_NOC_API_VERSION=1` only when that script or the kernel-op tests
say TTsim does not implement the default NoC API.

Test-specific variables are evidence, not global defaults. Inspect the tests, their conftest,
documented invocation and an applicable checked-in launch/task configuration. Add variables such as
PCC logging, forced program routes or DPRINT only when the tests require them; when tests need
different variables, run them as separate `kernel_tests.py run --only ... --env-json ...` batches. In particular,
never apply `TT_METAL_CORE_GRID_OVERRIDE_TODEPRECATE` to every Quasar test: some require the full
grid. When an override is required, record both its inclusive end coordinate and the resulting grid.

TTsim opens a local Unix socket. If a managed sandbox reports `Failed to bind simulation server
socket ... Operation not permitted`, rerun the same command with the required outside-sandbox
approval; do not classify that setup error as a kernel failure.

Run TTsim discovery, build, collection and pytest directly on the host with Bash, never through
`tt-device-mcp`, even when the user's instructions route every pytest through it: those rules
protect the physical device queue. A TTsim job in the broker blocks the real device, is cut off by
its job limits, and a killed job can leave the device held for every other user.
Do not run two TTsim jobs against the same build directory concurrently unless the simulator
documentation explicitly permits it. Collection is not a TTsim job: it imports ttnn but starts no
simulator, so `kernel_tests.py check` collects files in parallel.

## Kernel-op tests

The measured destination phase (`<plugin-root>/references/measured-destination.md`) runs one
kernel-op test per process with `kernel_tests.py`. TTsim proves execution and correctness on the
simulated architecture, never performance: do not report TTsim timing, throughput, host fallback
or footprint.

### Node IDs

Node IDs come from collection, not from reading the parametrize decorators:
- Fixtures such as `device_params` and an arch-parametrized `mesh_device` add ids
  (`[quasar-device_params0]`) that the decorators do not show.
- `tests/ttnn/conftest.py` defines `pytest_make_parametrize_id`; once a session loads it, every
  file in that session gets `name=value` ids. Collect each file in its own pytest process, as it
  will run.
- tt-metal's `pytest.ini` adds `-vvs`, so `-q` still prints a tree; `--verbosity=-1` prints ids.
- Ids are relative to the tt-metal checkout root, exactly as pytest prints them.

`kernel_tests.py check --fix-node-ids` does all of this. It repairs exactly one unambiguous prefix
added by collection, while ambiguous mismatches still stop. `run` refuses a node that is not
collected, so a wrong ID never becomes a result. A remaining `not found` is fixed in
`<target>:tests`, not recorded.

### Run time

TTsim simulates the chip at kHz clock rates, about a million times slower than silicon, so one
test can take hours. It prints only when a program is dispatched, never while kernels execute, and
it has no progress counter for tt-metal workloads. A silent log therefore means nothing: do not
kill a run for silence or elapsed time, and do not call a killed run a hang. The pinned
`pytest.ini` sets a pytest-timeout and tests add `@pytest.mark.timeout`; `kernel_tests.py`
disables the plugin (`-p no:timeout`) and sets no limit of its own, so add the same flag to any
TTsim pytest run outside it.
