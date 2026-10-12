# Tracing a ttnn call to its device op

Goal per call: device op struct (the Tracy `OP CODE`, e.g. `MatmulDeviceOperation`), `prim::`
name, program factory, call sites as `<path>:<line>`, input and output shape/dtype/layout/memory,
launches per inference, launch condition, and dispatch evidence (file and function or identifier,
with line).

## Procedure

1. Find the binding: `git grep -n "<api name>" <sha> -- ttnn/cpp ttnn/ttnn`.
2. Follow the C++ entry to every `ttnn::prim::` call and every helper that launches one
   (`to_memory_config`, `to_layout`, `reshape`, `concat`, `move`).
3. In the device operation, find `select_program_factory` and evaluate it for this input.
4. Note conditions that depend on runtime state (free L1, allocator addresses). A condition that
   picks the factory makes the row `unverified` with the alternatives in `dispatch_evidence`; a
   condition that decides whether the launch happens goes in `launch_condition`.

## Known traps (each caused a wrong count in a previous analysis)

- `ttnn.reshape` on ROW_MAJOR with the same last dim is a view: no launch.
- `ttnn.to_memory_config` returns the input unchanged when the configs compare equal: no launch.
  `ttnn.reshard` has no such check: it always launches.
- `to_layout(TILE, dtype=...)` with padding is one `tilize_with_val_padding`, not tilize + typecast.
- `ttnn.add`/`ttnn.mul_` all go to `binary_ng`; activations listed in the call are fused.
- `ttnn.linear` with only `core_grid` builds its own program config; bias is fused when a config
  or core grid is given.
- `split_query_key_value_and_split_heads` on a sharded input goes to `create_qkv_heads`;
  `concatenate_heads` goes to `nlp_concat_heads`.
- conv2d: 1x1 with stride 1 and no padding runs as `prim::matmul` (1D mcast for height-sharded,
  2D mcast for block-sharded input); every other conv is `prim::halo` then `prim::conv2d`. A conv
  reshards its input only with `reshard_if_not_optimal` or a failed shard check, and adds a
  `prim::move` when `deallocate_activation` is set. `reallocate_halo_output` defaults to true and
  adds a `prim::move` after halo; the move is skipped when the new buffer lands at the same
  address, so its `launch_condition` names that.
- `max_pool2d` defaults to bf16 ROW_MAJOR output; a following 1x1 conv then pays a tilize.
- `avg_pool2d` takes the global fast path only when the input allows it; otherwise halo + pool2d.
- Count totals by stage in a scratch list, then sum once. Do not add up by hand across messages.

## Row normalization and confidence

`op_table.csv` contains launching device-op variants, not every Python call. Preserve every
zero-launch decision in the inventory and in `call_trace.csv` notes or `summary.md` so the reader
can see why it was excluded. Typical zero-launch decisions are a view reshape, an equal-config
`to_memory_config`, an untaken conditional `reallocate`, metadata-only squeeze/unsqueeze and
`deallocate`.

Merge call sites into one row only when all dispatch-relevant facts match: device op, program
factory, input/output shape, dtype, layout, memory placement, grid/shard/program config, profile
statuses, launch condition and confidence. Sum their launches and list every call site. If any
fact differs, give the rows distinct, stable stage names (for example `attention QK matmul` and
`attention PV matmul`) so the schema key remains unique. Keep `device_op` and `program_factory`
canonical; put source paths, Metal generation details and commentary in `dispatch_evidence`, not
in those key columns.

Treat `unverified` as the default until the Python binding, composite helpers, primitive dispatch,
factory selector and exact input-dependent branch have all been followed. Before setting
`confidence=verified`, inspect the selected factory and every helper used to choose it. A choice is
runtime-dependent when it reads free/live L1 (`get_max_l1_space`, allocator
reservations), buffer addresses, allocator placement, optional preallocated outputs, or another
value unavailable from pinned source. Mark it `unverified` unless static bounds prove the same
factory for every possible runtime value. Name every possible factory and the deciding condition
in `dispatch_evidence`. A runtime-dependent factory may still have a verified launch count;
explain that distinction rather than upgrading the whole row. `confidence` describes dispatch only;
it never changes a target status.

Do not classify a call as a view, equality no-op or untaken branch from a common-case assumption.
Prove the exact shapes/configs and predicate at the pinned SHA. If the launch decision itself still
depends on runtime state, keep the potential launch, count it, and name the deciding condition in
`launch_condition`. The validator then reports the total as a range from the `always` launches to
all launches; quote that range in `summary.md`.

When subagents traced blocks, the main agent owns this normalization and must reconcile their
naming, duplicate variants, zero-launch decisions and totals before validation.

Subagent agreement is not evidence. The main agent must reopen pinned source for every definitive
claim during the re-check; analogous reports, earlier runs and another agent's conclusion only
identify what needs verification.

## Subagent contract (larger models)

Give each subagent one block (stem, one layer type, head), the pinned SHA, the profiles, and this
file. It returns only CSV rows in the `op_table.csv` and `call_trace.csv` schemas from
`<plugin-root>/references/schemas.md`, with `confidence` set honestly. No prose summary.

The same rules hold for every subagent the skill launches, including target-status research
(Quasar forks and tests):

- It does all of its work itself. It launches no subagents and no background tasks of its own.
  Put this sentence in its prompt.
- It returns once, when its whole task is finished, with one complete result. It never hands back
  partial results while work is still running.
- If its task is too large, the main agent splits it into more subagents of its own.
