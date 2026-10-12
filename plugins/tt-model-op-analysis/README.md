# tt-model-op-analysis

Kernel-op analysis of one implementation of a tt-metal model on the architecture it runs on, and
how its kernel ops would port to other architectures.

```bash
/plugin install tt-model-op-analysis@tenstorrent-skills     # Claude Code
codex plugin add tt-model-op-analysis@tenstorrent-skills    # Codex
```

One skill, `model-op-analysis`. A run is one implementation of a model, its entry test, a source
profile and a pinned tt-metal commit, stored in `op-analysis/<model>/<impl-slug>/<source>/<sha7>/`
with a `run.json`. A session starts by listing the model's existing runs; it continues one of them
or starts a new one, and never repeats work a run already has.

- **Source profiles:** `p100`, `p150` (Blackhole), `n150`, `n300-1chip` (one of the two
  Wormhole chips of an N300 card; single-device tests only) and `quasar` (TTsim).
- **Static analysis**, once per run: every ttnn call of the entry test traced to its device op and
  program factory, with support decided per kernel op. No device.
- **Destinations**, optional and added any time without re-tracing: another profile running the
  same code, or `quasar-bf16`, a Quasar projection with block-float dtypes as BF16 and mainline
  calls routed to their Quasar forks.
- **Measurements**, as items picked one or more at a time and resumable across sessions: a Tracy
  run of the entry test on a local source device (`source:tracy`), the entry test on TTsim for a
  Quasar source (`source:ttsim-e2e`), and TTsim runs of Quasar kernel-op tests grouped by row
  status (`<target>:kernel-tests ❌`). Each item shows its state and a time estimate.

Outputs in the run directory:

- `kernel_ops.csv`: one row per kernel op (device op and program factory) with launches and
  statuses per target; the summary starts with it;
- `op_table.csv`: one row per launching device-op variant, marked `static`, `measured` or
  `static+measured`;
- `call_trace.csv`, `port_blockers.csv`, `host_ops.csv`, `summary.md`;
- Tracy: `measured_ops.csv`, `host_fallback.csv`, `footprint.csv`;
- TTsim: `kernel_test_results_<target>.csv` with pass, fail or hang per kernel-op test.

Comparing two implementations of one model (for example the Blackhole model and its Quasar port)
is a separate step on two runs.

Setup asks one question at a time, using native choice controls when the host exposes them. Counts,
totals, schema checks, merges and test runs come from the scripts in `scripts/`, not from the agent.
Tracy needs a device of the source architecture (routed through `tt-device-mcp`); Quasar items need
a local TTsim build, which gives execution and correctness evidence, never performance. Upload to
Google Drive happens only when the host has a Drive/Sheets connector and the user agrees.
