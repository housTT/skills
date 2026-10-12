# Targets and profiles

A static run compares exactly two targets:

- **source**: where the analysed code is expected to work today. The implementation and entry
  test are chosen for it, and its launches are traced.
- **destination**: where the code would be ported. Its columns say what happens to each source
  op there.

The user picks both; neither has a default. Source and destination must differ. To compare more
targets, make another run.

| Target | Question its status answers | Allowed as | Profile |
|---|---|---|---|
| `p100` | Does this op run on P100 as written? | source, destination | `p100` |
| `p150` | Does this op run on P150 as written? | source, destination | `p150` |
| `quasar` | Does this op run on Quasar with the code unchanged (same dtypes, grids, mainline ttnn calls)? | destination | `quasar` |
| `quasar-port` | Does the existing Quasar port of this model cover this function, and with which op? | destination, only when a port exists in the tree | `quasar` |
| `quasar-bf16` | Would this op run on Quasar if block-float dtypes became BF16 and mainline calls used their `ttnn.experimental.quasar.*` forks, everything else unchanged? | destination | `quasar` |

Status and evidence rules for every target are in `<plugin-root>/references/target-status.md`.

## Profiles

Each profile fixes the values the model code branches on. Confirm its values at the pinned commit
(source files below) and record them in `run.json` under `profile_values`.

| Profile | arch | Tensix compute grid | DRAM grid x | Where to confirm |
|---|---|---|---|---|
| `p150` | blackhole | 13x10 = 130 | 8 | `tt_metal/core_descriptors/blackhole_140_arch.yaml` (WORKER dispatch) |
| `p100` | blackhole | 11x10 = 110 | 7 | `models/common/device_utils.py` (`dram_grid_size.x == 7` means P100) |
| `quasar` | quasar | 8x4 = 32 (ttsim) | n/a | `tt_metal/soc_descriptors/quasar_32_arch_ttsim.yaml`, `tt_metal/core_descriptors/quasar_simulation_8x4_arch.yaml` |

## Evaluating branches

In the inventory step, list every predicate the model and its configs read, and evaluate it per
profile. Common ones:

- `is_blackhole()`, `is_wormhole_b0()`: arch only. On Quasar both are false, so code falls to
  the "else" branch, which is often a Wormhole-or-generic path.
- `is_blackhole_p100(device)` in the ResNet50 model utils is `grid != 130`; any non-130 grid
  takes the P100 branch, including a P150 run with ETH dispatch (14x10).
- `device.compute_with_storage_grid_size()` and `dram_grid_size()` feed core counts and shard
  specs; compute them per profile.
- Hardcoded `CoreGrid(...)` / `CoreCoord(...)` larger than a profile grid: record the value in
  `grid_dependency` and, for the destination, in `port_blockers.csv`.

## Quasar grid override (measured mode, informational)

`TT_METAL_CORE_GRID_OVERRIDE_TODEPRECATE` takes the inclusive END coordinate `[x, y]`, not a size.
The 8x4 descriptor starts at `[2, 2]`, so `"3, 2"` gives a 2x1 grid. Always confirm the grid the
device reports after the run.
