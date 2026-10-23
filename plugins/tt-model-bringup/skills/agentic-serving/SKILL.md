---
name: agentic-serving
description: Optimize a bringup for the agentic serving workload it will actually see (many concurrent multi-turn conversations with long shared prefixes), keep device memory accounted for, and run budgeted agentic evals (goal 12). Use whenever a stage chooses layouts, precision, memory reserves, batch size, cache policy or serving configuration, and for goal 12.
---

# Agentic serving

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Stages record what this skill
asks for in `models/autoports/<model>/doc/agentic_serving.json`; `scripts/check_agentic_serving.py`
gates it. Missing evidence is critical. A measured shortfall is critical unless it carries a reason
(`copy_reason`, `pool_shortfall_reason`, `serving.reason`, `trace_reason`), which makes it advisory.

## Target workload

Agents resend long conversations with short appends and wait on each reply; several share one
engine. Throughput then depends on prefix-cache reuse, how many conversations the KV pool holds,
per-step fixed costs, and long-context accuracy, none of which a 128/128/1 benchmark shows.

Record `workload`: `target_concurrency`, `working_set_tokens` (live conversation per agent),
`typical_context_tokens`, and `source`. Without measurements, estimate them from the intended evals
and say so; goal 12 replaces them with measured values. Every stage that reports performance reports
the target workload next to batch 1, which stays as a regression row.

## Memory

Decode on large models is usually weight-read bound, so extra rows are cheap in time and expensive
in KV memory. Weights, KV pool, per-request reserves and trace region compete for the same DRAM.
From stage 06 on, keep `ledger`: per weight family its bytes and `copies` (a second copy needs a
measured `copy_reason`), reserves against their measured peak use, KV bytes per token, and
`pool_tokens`. The pool should hold `target_concurrency × working_set_tokens`; otherwise record a
`pool_shortfall_reason`. Reconcile the ledger with allocator readings and explain any gap.

## Serving invariants

Check these on replayed or synthetic multi-turn traffic at the target concurrency, not only on
fixed-shape benchmarks. Record them under `serving` in stages 09 and 10.

- `prefix_caching`: enabled for serving, with a hit rule for every layer kind (full attention,
  sliding window, recurrent state). Otherwise `false` with a `reason`.
- `hit_fraction_of_ceiling`: cached tokens over the tokens a previous turn already sent. It should be
  at least 0.9 unless live conversations exceeded the pool (`pool_bound: true`). Look at which
  prefixes are lost and why: by time away, by previous reply length, and by pool pressure.
- `trace_captures_after_warmup`: captures, retirements and evictions per request, all 0.
- No host work per decode step that scales with steps or pool size.
- Start-up stays small relative to an eval suite.

Precision is part of this. Short-prompt top-k agreement does not show long-context or free-running
failures (malformed tool calls, loops to the length cap). Stage 08 also checks agent-shaped long
prefixes and free-running replies before choosing.

## Look at the whole run

Before changing anything, draw what a run did over time: device activity, what requests waited on,
memory and cache fill. Then ask where time goes, what sits idle, what work repeats, and how the end
differs from the middle. Name a mechanism, predict the effect of a change, and check it on the next
run.

## Goal 12: agentic evals

Optional operator inputs come from `bringup/agentic_evals/inputs.json` in the target checkout:

- evals: default `r1_gpqa_diamond`, `swe_bench_verified`, `terminal_bench_2`, the release-spec
  names; the default subset is 10 tasks each;
- `batch_sizes`, default 1, 8, 16, 32, shared with stages 10 and 11: stage 10 validates a tuned
  configuration for each, stage 11 measures performance across all of them, and evals run once on
  the largest the device holds — 16 rather than 32 is an acceptable recorded outcome;
- `stage_budget_hours` (default 24) and `suite_budget_hours` (default 8);
- reference scores;
- the eval harness: default the tt-inference-server agentic runner;
- `compute`.

`compute` is the hardware the operator grants, on top of the local device, which stays available
for the quick checks the earlier stages ran there. Without a grant, local is all there is. With
one, `max_parallel` is how many runs may be in flight: a ceiling to fill when the plan needs it,
not a quota to spend. Re-plan when the grant changes.

A grant dispatches runs one of two ways. `commands` takes operator-supplied `submit`, `status` and
`collect` shell commands and is the general form. `kind: "github_actions"` dispatches a workflow in
a CI repository, which needs the implementation pushed where the runners can fetch it; see
[CI compute](references/ci-compute.md) for the input shape, the publish contract and the polling
loop. Either way, record the grant you actually used in `doc/agentic_evals/compute.json` so a
reader can tell a thin result from a small grant.

Work from cheap to expensive:

1. Prove the harness end to end on every runner, with a few tasks per eval. On a CI grant this is
   also what proves the published refs: a run that fetches the pushed branches and serves the model
   is the only evidence the publish was complete.
2. Run small seeded samples at the stage-11 configuration with caching on. Measure the workload and
   noise, and check the invariants on the real traffic. Send failures back to the stage that owns
   them.
3. Run rounds of arms. Each arm makes one change from the current best and runs on the same tasks,
   with its prediction written first. Keep a change only when it beats the noise and its mechanism
   explains the gain.
4. Re-run the combined changes together. Changes that draw on the same memory interact.
5. Estimate each full suite from the samples, with a range. Run it only if the estimate fits
   `suite_budget_hours` before the deadline. A measured stop is a valid result.
6. When exploration ends, run the full suites on the best version, split so each dispatch finishes
   inside the grant's per-run timeout. If the subsets plus the stage-11 benchmarks fit one run,
   add a release run with per-eval timeouts summing under the cap.

### Fanning out

Run arms in parallel when the grant allows it. An explorer owns one hypothesis and iterates on it:
predict, size, dispatch, wait, then read the result against the estimate it wrote first. It runs
its own thread until the hypothesis settles or its budget ends, then reports the arm with its
evidence.

A coordinator holds the current best, the slot ledger and the budget, decides what is kept, and
re-runs kept changes together. Explorers do not accept their own arms; every dispatched run
registers with the coordinator at launch, not at completion.

The coordinator reads wide where an explorer reads deep — everything the runs leave behind, across
arms, the way [Look at the whole run](#look-at-the-whole-run) asks — and sets what to probe next.
Not every run has to be a candidate for best: one that only locates headroom or rules a mechanism
out earns a slot when the picture is thin.

Give explorers different mechanisms, not one idea at several magnitudes.

### Sizing a sample

Estimate a sample's CI time before dispatching, and size it to what it has to separate. Too small
to beat the noise decides nothing; a full suite to settle what a sample answers costs more.

Shape follows the claim: concurrency needs enough simultaneous conversations to load the pool and
scheduler, precision and long context need long free-running replies, caching needs repeated
multi-turn traffic. A count valid for one is meaningless for another.

Write these to `doc/agentic_evals/`:

- `plan.json`
- `runs/<arm>/` (manifest and collected results)
- `estimate.json`: per eval `decision` (`go` or `stop`) and `reason`
- `full/<eval>/results.json` with `actual_hours` next to the prediction
- `serving_config.json`: the measured `max_concurrency`, `prefix_caching`, `max_num_seqs` and agent
  timeouts that a release spec should use
- `compute.json`: the grant as used — `max_parallel`, peak runs in flight, and for a CI grant the
  published refs and every dispatched run id, so each result traces back to a run
- `REPORT.md`

Update `workload` with the measured values.
