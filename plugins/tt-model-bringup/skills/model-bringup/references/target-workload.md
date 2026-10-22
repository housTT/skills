# Target workload and evidence

Use this reference when choosing performance targets or validating full-model,
precision and serving changes. Keep the record in `doc/workload.md` beside the
existing context contract; link raw evidence rather than copying logs into it.

## Establish the target

Record the intended workload, representative prompt and output lengths, concurrent
requests, typical retained context, reuse pattern, generation policy and metric to
optimize. Separate client concurrency, server capacity and physical active rows.
Use the user's stated target. Otherwise retain the existing single-user baseline
and record assumptions; do not assume every model is intended for agentic use.
For multi-turn workloads, measure useful end-to-end work at the target concurrency
and context. Keep the 128/128 single-user and CI burst rows as regression/comparison
rows, not substitutes for the target. Select physical batch buckets from measured
latency and memory; preserve supported capabilities beyond benchmark profiles.

## Reuse representative inputs

Before full-model optimization, select a small, versioned set of representative
inputs from authorized local data or public fixtures. Store messages and tool
schemas, provenance and a content hash in the run workspace; render with the
selected model's native template. Keep private trajectories out of this plugin.
Include short and long contexts, small appends to long prefixes, mixed lengths,
partial batches and slot reuse. For each layer kind, note required KV/recurrent
state, absolute positions, chunk continuation and lifetime at a cache hit.
Use the existing generator/serving harness to replay these inputs; record commands,
rendered token counts, outputs and implementation identity. Fixed transcript replay
checks execution and continuation, not whether a live agent can solve a task.
Refresh the sample if live traffic has materially different lengths or behavior.

## Account for memory (full model and precision changes)

In `doc/workload.md`, keep a per-device/per-bank table of weights by family, KV or
recurrent state, page tables, scratch/history slots, trace/CCL buffers and runtime
reserve. Count shared allocations once; justify duplicate prefill/decode layouts
with measured latency and memory cost. Reconcile totals with allocator checkpoints
after loading, cache allocation and warmup, explaining the residual and measurement
tolerance. Query allocator statistics at checkpoints, not every decode step.

Record pool capacity and measured simultaneous state/history use. For full-attention
KV, compare pool tokens with the sum of live conversation lengths at target
concurrency; use architecture-specific accounting for sliding-window/recurrent
layers. One sequence fitting does not prove concurrent capacity. Size reserves to
observed overlap plus a stated margin, and test exhaustion/backpressure. Recompute
for the selected precision and a practical higher-precision fallback: weight bytes,
cache capacity and conversation length can all change. Preserve context_contract.json
as the authority for supported context; this table explains workload capacity.

## Validate long-context quality (datatype sweep)

Alongside the existing top-k gate, compare the baseline and final precision candidate
on the same short and long representative prefixes using HF controls. Include
continuation/tool-format cases when relevant. Record top-1 agreement and, when logits
are available, KL at identical token positions, plus generated repetition, malformed
output and capped-response counts. Separate template/integration errors from numeric
loss. Define tolerances before selection using the baseline/reference variation;
there is no universal KL or loop threshold. Small screens order candidates; validate
the selected full-model path. A short top-k pass cannot waive a long-context failure.
BFP4/LoFi is a candidate, not an acceptance requirement. Evidence of model-visible
loss can justify higher precision without repeatedly reopening the rejected policy.

## Exercise reuse and overhead (serving stages)

For workloads with reusable prefixes, implement and validate caching, then enable
it for the target profile; record an unsupported path as a remaining capability gap.
Compare cached and uncached continuations with the same inputs and generation policy.
Exercise a long reply followed by an append, interleaved requests, slot reuse and
pool pressure. Check every layer kind's required window/state; classify misses by
capacity, eviction or unsupported state rather than assuming a specific anchor fix.
Compare observed hits with the actually reusable prefix, not total input tokens.

Replay mixed cached/chunked prefills after warmup and verify existing trace-lifetime
requirements with capture/retirement counters. Measure small-append prefill, decode
step time versus active rows at typical context, queueing, sampling/host work and
startup (load, compilation, capture). Investigate fixed host costs before retuning
matmuls. Stage only justified shapes and report startup separately from warmed
performance, including it in estimates of complete evaluation wall time.

Keep these checks in existing stage evidence and have stage-review inspect the raw
artifacts. Missing measurements are missing evidence, not a prose-based pass.
