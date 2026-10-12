# tt-model-op-analysis

Kernel-op analysis of one tt-metal model test and how its kernel ops port to a destination
architecture.

```bash
/plugin install tt-model-op-analysis@tenstorrent-skills     # Claude Code
codex plugin add tt-model-op-analysis@tenstorrent-skills    # Codex
```

One skill, `model-op-analysis`, compares a source target (P100 or P150), where the model runs
today, with a destination target (P100, P150, Quasar with the code unchanged, an existing Quasar
port, or a Quasar BF16 projection). It works in three modes:

| Mode | What it does |
|---|---|
| static | Traces every ttnn call of the entry test at a pinned commit to its device op and program factory, and decides support per kernel op on both targets from source and tests. No device. |
| static + measured | Static, then optional measured phases: a Tracy run of the same entry test on a local source device, and TTsim runs of the destination's kernel-op tests. |
| measured over an existing static run | Adds the measured phases to an earlier static run, on that run's commit. |

Outputs, in `op-analysis/<model>/static/<date>_<sha7>/` with a `run.json`:

- `kernel_ops.csv`: one row per kernel op (device op and program factory) with launches and
  destination statuses; the summary starts with it;
- `op_table.csv`: one row per launching device-op variant, marked `static`, `measured` or
  `static+measured`;
- `call_trace.csv`, `port_blockers.csv`, `host_ops.csv`, `summary.md`;
- measured source: `measured_ops.csv`, `host_fallback.csv`, `footprint.csv`;
- measured destination: `kernel_test_results.csv` with pass, fail or hang per kernel-op test.

Setup asks one question at a time, using native choice controls when the host exposes them. Counts,
totals, schema checks, merges and test runs come from the scripts in `scripts/`, not from the agent.
Measured phases need a device of the source architecture (routed through `tt-device-mcp`) and, for
a Quasar destination, a local TTsim build; TTsim gives execution and correctness evidence, never
performance. Upload to Google Drive happens only when the host has a Drive/Sheets connector and the
user agrees.
