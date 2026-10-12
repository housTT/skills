# Targets and profiles

A run has one **source** and zero or more **destinations**:

- **source**: the profile the analysed implementation runs on today. The implementation and entry
  test are chosen for it, and its launches are traced.
- **destination**: another profile the same kernel ops are projected onto, from the source trace,
  without tracing again. Its columns say what happens to each source op there.

The user picks the source; destinations are optional and added one at a time. A destination is a
profile other than the source running the same code, or `quasar-bf16` (only for a non-Quasar
source). To analyse a different implementation, such as an existing Quasar port, make a run with
that implementation as its source.

| Target | Question its status answers | Allowed as | Profile |
|---|---|---|---|
| `p100` | Does this op run on P100 as written? | source, destination | `p100` |
| `p150` | Does this op run on P150 as written? | source, destination | `p150` |
| `n150` | Does this op run on an N150 (one Wormhole chip) as written? | source, destination | `n150` |
| `n300` | Does this op run on one chip of an N300 as written (single-device tests only)? | source, destination | `n300` |
| `quasar` | Source: does this kernel op run on Quasar? Destination: does it run on Quasar with the code unchanged (same dtypes, grids, mainline ttnn calls)? | source, destination | `quasar` |
| `quasar-bf16` | Would this op run on Quasar if block-float dtypes became BF16 and mainline calls used their `ttnn.experimental.quasar.*` forks, everything else unchanged? | destination, non-Quasar source only | `quasar` |

Status and evidence rules for every target are in `<plugin-root>/references/target-status.md`.

Multi-chip setups (N300 as a two-chip mesh, QuietBox, T3K, Galaxy) are out of scope: they add CCL
ops and per-device versus per-inference counting. An `n300` run uses tests that open one device.

## Profiles

Each profile fixes the values the model code branches on. Confirm its values at the pinned commit
(source files below) and record them in `run.json` under `profile_values`.

| Profile | arch | Tensix compute grid | DRAM grid x | Where to confirm |
|---|---|---|---|---|
| `p150` | blackhole | 13x10 = 130 | 8 | `tt_metal/core_descriptors/blackhole_140_arch.yaml` (WORKER dispatch) |
| `p100` | blackhole | 11x10 = 110 | 7 | `models/common/device_utils.py` (`dram_grid_size.x == 7` means P100) |
| `n150` | wormhole_b0 | 8x8 = 64 | 12 | `tt_metal/core_descriptors/wormhole_b0_80_arch.yaml`, board `nebula_x1`; DRAM views in `tt_metal/soc_descriptors/wormhole_b0_80_arch.yaml` |
| `n300` | wormhole_b0 | 8x7 = 56 per chip | 12 | same file, board `nebula_x2`; `models/common/device_utils.py` names two devices N300 |
| `quasar` | quasar | 8x4 = 32 (ttsim) | n/a | `tt_metal/soc_descriptors/quasar_32_arch_ttsim.yaml`, `tt_metal/core_descriptors/quasar_simulation_8x4_arch.yaml` |

## Evaluating branches

In the inventory step, list every predicate the model and its configs read, and evaluate it per
profile. Common ones:

- `is_blackhole()`, `is_wormhole_b0()`: arch only. `is_wormhole_b0()` is true on `n150` and
  `n300`. On Quasar both are false, so code falls to the "else" branch, which is often a
  Wormhole-or-generic path.
- `is_blackhole_p100(device)` in the ResNet50 model utils is `grid != 130`; any non-130 grid
  takes the P100 branch, including a P150 run with ETH dispatch (14x10).
- `device.compute_with_storage_grid_size()` and `dram_grid_size()` feed core counts and shard
  specs; compute them per profile.
- Hardcoded `CoreGrid(...)` / `CoreCoord(...)` larger than a profile grid: record the value in
  `grid_dependency` and, for a destination, in `port_blockers.csv`.

## Quasar grid override (measured mode, informational)

`TT_METAL_CORE_GRID_OVERRIDE_TODEPRECATE` takes the inclusive END coordinate `[x, y]`, not a size.
The 8x4 descriptor starts at `[2, 2]`, so `"3, 2"` gives a 2x1 grid. Always confirm the grid the
device reports after the run.
