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
  that fork instead. A mechanical substitution to the fork's corresponding program-config type is
  part of that routing only when its fields map one-to-one; all values and topology stay unchanged.
  No retuning, algorithm change or missing argument may be assumed. Grids, shard specs and program
  config values stay as written; grid blockers are reported only in `quasar_blockers.csv`.
  Evidence in `quasar:bf16_projection_evidence` names the dtype change, fork and config-type mapping,
  or says none applies. Uses `✅`, `⚠️`, `❌`, `n/a` with the meanings below; `✅` here means a Quasar
  test covers the projected variant, not that the projected model runs.

| Value | Meaning | Evidence required in `quasar:evidence` |
|---|---|---|
| `✅` | A Quasar test covers the same op semantics, factory variant, layout and dtype (memory placement may differ), or `run.json` records a passing port e2e run that executes it | test path and matching parameters/variant, or "port e2e" plus its `run.json` date |
| `⚠️` | Code path exists (Metal 2.0 mainline factory or a Quasar fork) but this variant has no Quasar test | the factory or fork path |
| `❌` | The exact variant is directly proven not to complete correctly: API/binding mismatch, validation failure, TT_FATAL, or an exact reproducer/result showing a deterministic hang or wrong result | the failed signature/check or exact reproducer/result (also required in `quasar:bf16_projection_evidence`) |
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

## Coverage matching and consistency

Do not promote a row to `✅` because a test exercises only a similarly named operation. Match the
semantics that affect dispatch: required arguments and output behavior, in-place versus out-of-place,
broadcast mode, sharding/layout, dtype, fused bias/activation, and factory family (for example 1D
versus 2D multicast). A test of fold without `collapse_output` does not cover a call that requires
`collapse_output`; a 1D-mcast linear test does not cover a 2D-mcast linear. Memory placement alone
may differ as stated above.

If a fork lacks a required argument or has no one-to-one program-config type, the projection is
`❌`; the signature mismatch is direct evidence. Do not silently redesign the model. If the path
exists but merely lacks exact coverage, use `⚠️`. A comment that a related factory hangs supports
`⚠️`, not `❌`, unless it identifies an exact reproducer/result for the same dispatch-relevant
signature. When that exact evidence exists, use `❌` consistently for every matching row. A failure
for one factory, broadcast mode, sharding or fused variant must not be copied to another.

Use this conservative order: exact passing test or recorded port e2e -> `✅`; exact direct failure
-> `❌`; everything else -> `⚠️`. Absence of a test is never evidence of failure, and existence of
a similarly named test is never evidence of support.

Before finishing, group rows by the Quasar-relevant signature (operation semantics, factory,
layout, dtype and sharding). Equal signatures must have equal statuses unless the evidence states
the row-specific distinction.
