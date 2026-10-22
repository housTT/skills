# Bringup pipeline improvements

Review basis: the complete supplied `bringup-plan.html` (Agentic Bringup Plan),
`tenstorrent/skills` main at `48933af4`, and local batching commit `b621b10c`.
The plan's experiment figures motivate checks; they were not independently
remeasured and are not universal acceptance thresholds.

## Selected local changes

Retain performance-before-accuracy ordering, prior-measurement expectations,
concurrency selection from useful wall time, and linked server/measurement evidence.
Remove the requirement that all models implement physical buckets 1/8/16 and cap
active device batches at 16. Retain the established readiness-server default.
Consolidate the repeated batching guidance into the performance-review reference.
These selected changes form the first commit above current main.

## Applying the HTML recommendations

| Recommendation | Decision and implementation |
|---|---|
| Representative replay corpus and runner | Require a small versioned input set, native-template rendering, exact replay commands and raw outputs in the run workspace. Reuse existing generator/serving harnesses. Do not distribute private trajectories or invent a new corpus service. |
| One target-workload definition | Add `skills/model-bringup/references/target-workload.md`, linked by owning stages. Replace repeated single-user-primary mandates; keep single-user and CI rows as comparable regressions. |
| Memory ledger and single weight copy | Require per-device/bank accounting, allocator checkpoints, shared-allocation accounting, duplicate-layout justification and concurrency-aware capacity in existing reports. Use architecture-specific state accounting; concurrency times context is not valid for every hybrid model. |
| Prefix caching and trace reuse | Require cache-on correctness for reuse workloads, long-reply/append and pressure cases, per-layer state accounting, and trace counters. Preserve existing trace-lifetime requirements. Unsupported caching stays an explicit capability gap. |
| Long-context precision selection | Add representative short/long HF comparisons and generated-output checks beside top-k acceptance. Let real quality failures reject BFP4/LoFi. Reconcile precision with concurrent memory capacity. |
| Small-tail prefill, startup and host overhead | Add these measurements to target-workload serving evidence. Use observed overlap for reserves and checkpoint allocator queries. Avoid universal startup percentages or model-specific latency floors. |
| Configurable benchmark profiles and cache mode | Implement `performance_concurrencies` through runner, evaluator, report and offline gate. Keep default 1/8/16 and the one-hour cap. Allow explicitly validated cache-on accuracy; random 4096-token performance rows remain cache-off. |
| Goal 12 and estimate-before-full-eval | Add the bounded sample/confirm/estimate/full-run procedure to existing TTI release guidance. Do not automatically add a 24-hour goal, scheduler, statistical estimation framework or new resource grant. |
| Pipeline compute inputs | Record granted resources and budget in the release procedure. Changes to the separate orchestration repository remain follow-up work. |
| Measured TTI configuration | Release guidance uses measured capacity, cache mode, reply lengths and task times, with unmeasured fields labelled. |
| Shield collection and comparison | Require collecting/interpreting dispatched results and comparing matching tasks. Wiring the separate pipeline's dispatch/wait code remains follow-up work. |

## Limits and follow-up

The new workload, memory and precision requirements use existing stage-review and
stage reports. They are not new executable memory/KL/cache gates. Adding a checker
that only trusts a self-reported `passed` field would not establish correctness.
Reusable device instrumentation and a stable replay corpus should precede those
machine-enforced gates. Cache behavior, long-context quality and performance still
need model/hardware measurements; offline tests establish workflow integrity only.

The fixed-length benchmark can estimate useful concurrency, but it does not model
multi-turn cache reuse or agent task quality. Use the representative pilot and
record the remaining limitation when its best configuration differs from the
cold-prefill estimate. Do not transplant Gemma's noise range, sample sizes, timing
budget, memory tolerance or score threshold to another model.

A future standalone agentic-evaluation pipeline can reuse the release procedure
once corpus ownership, reference scores, dispatch backends and compute policy are
settled. Its completion must distinguish full measured results from a reasoned
stop with partial results. This change keeps those larger decisions explicit.
