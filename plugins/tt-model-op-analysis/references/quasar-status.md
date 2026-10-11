# Quasar status rules

Three status columns, each answering a different question:

- `quasar:as_written`: the analysed code unchanged on Quasar (same dtypes, grids and mainline
  ttnn calls). Evidence in `quasar:evidence`.
- `quasar:port`: only when a Quasar port of the model exists in the tt-metal tree, that port's
  implementation of the same function. All `n/a` when no port exists. Evidence in
  `quasar:evidence`.
- `quasar:bf16_projection`: a projection, not existing code. The status the op would have if the
  analysed code changed in exactly two ways: every block-float dtype (`bfloat8_b`, `bfloat4_b`)
  becomes `bfloat16`, and every mainline call that has a `ttnn.experimental.quasar.*` fork calls
  that fork instead. Grids, shard specs and program configs stay as written; grid blockers are
  reported only in `quasar_blockers.csv`. Evidence in `quasar:bf16_projection_evidence`, naming
  the dtype change and the fork used, or saying none applies. Uses `✅`, `⚠️`, `❌`, `n/a` with the
  meanings below; `✅` here means a Quasar test covers the projected op, layout and bf16 dtype, not
  that the projected model runs.

| Value | Meaning | Evidence required in `quasar:evidence` |
|---|---|---|
| `✅` | A Quasar test covers the same op, layout and dtype (memory placement may differ), or `run.json` records a passing port e2e run that executes it | test path, or "port e2e" plus its `run.json` date |
| `⚠️` | Code path exists (Metal 2.0 mainline factory or a Quasar fork) but this variant has no Quasar test | the factory or fork path |
| `❌` | Fails validation or hits TT_FATAL on Quasar | the check that fails (also required in `quasar:bf16_projection_evidence`) |
| `variant not used by port` | The port implements the function differently (for example height- instead of block-sharded) | what the port uses instead |
| `n/a` | The port never reaches this path | why |

Facts to verify at the pinned commit before relying on them:

- Block-float formats: `is_supported_quasar` in `tt_metal/common/tt_backend_api_types.cpp` lists
  the formats Quasar accepts; at the time of writing it has no `Bfp8_b`/`Bfp4_b`.
- Legacy kernels: `DataMovementKernel`/`ComputeKernel` constructors in
  `tt_metal/impl/kernels/kernel.hpp` TT_FATAL on Quasar, so ProgramDescriptor/CreateKernel
  factories cannot run there; only Metal 2.0 ProgramSpec factories lower to Quasar kernels.
- Quasar forks live under `ttnn/cpp/ttnn/operations/experimental/quasar/` and are exposed as
  `ttnn.experimental.quasar.*`; mainline calls are not routed to them automatically.
- Quasar op tests are mostly outside CI (`tests/scripts/quasar/*.yaml`); say so when a ✅ rests on
  a locally run test.
