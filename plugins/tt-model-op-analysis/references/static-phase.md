# Static phase

Reads code only; never touches a device. Every count must survive
`<plugin-root>/scripts/validate_static.py`, and every dispatch claim carries evidence.

## Steps

1. **Source and blocks.** Load `<plugin-root>/references/targets.md`, confirm the source profile's
   values at the pinned SHA, and write `source`, `destinations` (empty), `implementation`,
   `profile_values` and `entry_test_passes` to `run.json`. Write `stage_blocks`: the model's
   top-level blocks in forward order, from its structure (for ResNet50 `stem`, `layer1` …
   `layer4`, `head`).
2. **Inventory.** Read the model file and the test once, by range, noting line numbers. List every
   ttnn call on the forward path (`<file>:<line>`, stage, how many times it runs per inference),
   including calls that become views, equality no-ops or untaken branches, and every predicate from
   targets.md the code reads. Note host-side work for `host_ops.csv`. Keep zero-launch decisions in
   the inventory and summary/call-trace notes, not as device-op rows in `op_table.csv`.
3. **Trace.** Load `<plugin-root>/references/dispatch-tracing.md`. Small model: trace yourself.
   Larger model: one subagent per block with the subagent contract in that file. Collect rows for
   `op_table.csv` and `call_trace.csv` per `<plugin-root>/references/schemas.md`, then normalize
   duplicate call sites and zero-launch decisions yourself before writing either table. Every
   traced row has `origin` `static`. Stages follow `<block> <module> <role>` (schemas.md), so runs
   of two implementations of one model align by stage.
4. **Source status.** Load `<plugin-root>/references/target-status.md` and fill the source's
   `<t>:status` and `<t>:evidence`. For a Quasar source fill `<t>:tests` with the node ids of the tests that
   decide each status and set `<t>:test_result` to `not run`. Research subagents follow the
   subagent contract in dispatch-tracing.md.
5. **Re-check evidence.** Reopen the pinned source for every `verified` row and every status. A
   status follows its definition in target-status.md; Quasar targets decide per kernel op (device
   op, factory, layout, dtype) from the Quasar tests and fork code. Dispatch uncertainty
   (runtime-dependent factory, conditional launch) belongs in `confidence` and `launch_condition`;
   do not move a status to `⚠️` for it. `verified` requires the complete dispatch chain for the
   exact variant. Apply the same status to rows sharing the target-relevant signature unless
   row-specific evidence distinguishes them.
6. **Gate.** Run `python3 <plugin-root>/scripts/validate_static.py <run dir>`. Fix and rerun until
   it prints `OK`. Resolve every warning or say in `summary.md` why the repeated text is correct.
   No output, summary or upload before that.
7. **Kernel ops and summary.** Run `python3 <plugin-root>/scripts/kernel_ops.py <run dir>`. Write
   `summary.md`, starting with the per-kernel-op table from `kernel_ops.csv` (kernel op, launches,
   op-table rows, statuses per target), then: model, implementation, test, SHA, source and every
   destination with the question each answers, launches per inference per profile as the range the validator printed,
   count of each status per target, top port blockers, open `unverified` items. End with a "How to read this" section: every column of
   every CSV and every status value used, in plain words, including what each destination
   assumes. Update `summary.md` whenever a destination or a measurement is added.

## Rules

- Read sources as `git show <sha>:<path>` with line ranges; never the working tree. The only
  exception is an approved temporary test in the run directory (run-setup.md, section 1).
- In steps 2-5, read no file of an earlier run under `op-analysis/`: an earlier run is not
  evidence. Only `compare_runs.py` opens one, on request (run-setup.md).
- A status of `✅` needs evidence a reader can open. Without a matching test or run, `⚠️` with
  what is missing.
- Do not upgrade confidence or status because a likely branch, familiar operation name or
  analogous test looks convincing.
- `validate_static.py` is a structural/count gate, not evidence that a factory choice or support
  status is correct. The manual re-check in step 5 remains mandatory after it prints `OK`.

## Adding a destination

A destination reuses the source trace; it never re-traces the source. Ask for the profile (any but
the source, likely ones first) and, for a non-Quasar source with Quasar chosen, whether the code is
`same` (`quasar`) or a BF16 projection (`quasar-bf16`).

1. Append the destination to `run.json` `destinations`.
2. For a Blackhole or Wormhole destination running the same code, evaluate the profile predicates
   from targets.md for every source row and fill `<dest>:shapes` and `<dest>:launches`; add its
   `call_trace.csv` rows with `profile` set to it. Rows the destination does not launch get 0
   launches and `n/a`. Never change the source's columns or rows.
3. Fill `<dest>:status` and `<dest>:evidence` (and for Quasar targets `<dest>:tests`, with
   `<dest>:test_result` `not run`) per target-status.md; add the destination's blockers to
   `port_blockers.csv`. Measured-only rows get the destination's status from a kernel-op lookup.
4. Rerun `validate_static.py` and `kernel_ops.py`, then update `summary.md`.
