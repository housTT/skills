# Bounded agentic evaluation

Use this procedure when the requested release includes long-running agentic suites.
Keep Stage 11's one-hour benchmark cap and frozen protocols. Reuse the existing TTI
harness and authorized local/CI execution path; do not introduce a second scheduler.

1. Record the requested suites, comparable reference scores (or unavailable),
   sampling/tool policy, task IDs/seed, wall-clock budget and already-granted hardware.
   Do not infer permission to reserve additional machines. Preserve per-task results
   and exact model/harness/config identities in the release evidence directory.
2. Preflight a few tasks end to end. Check required images, disk and connectivity;
   measure server startup and verify the generated autoport is served. Diagnose
   infrastructure failures before retrying; keep failed attempts separate from scored
   results and clean up only owned resources.
3. Run a small fixed sample with the validated serving configuration. Compare live
   context/reply lengths with `doc/workload.md`. Inspect cache reuse, trace captures,
   active rows, startup and tail time, repetition, tool-format errors and truncations.
   Send correctness failures back to their owning stage before spending on tuning.
4. If tuning is needed, state one predicted improvement per candidate and compare the
   same tasks, policy and baseline. Confirm promising changes on a larger sample;
   repeat close comparisons to measure noise. Reconcile combined memory and rerun
   combined changes. A replay speedup is not an agent-task score improvement.
5. Before a full suite, record an estimate range from measured task times, startup,
   concurrency and tail, accounting for differences in full-set lengths. Reserve time
   for reporting/recovery and stop iteration while the full run can still fit. No
   Gemma-specific noise floor, sample size or time threshold is a default. If the
   estimate exceeds the remaining budget or quality clearly fails the agreed target,
   report the gap and sample results instead of labelling the suite complete.
6. Collect and interpret every dispatched run. Retain partial outcomes on failure;
   re-estimate as results arrive and stop owned work if the remaining budget cannot
   cover it. Record predicted versus actual duration, per-task scores, failure reasons
   and reference comparability. A submitted job is not a completed evaluation.

Populate release configuration from measured concurrency, context, caching, reply
lengths and task durations. Label unsupported or unmeasured fields. Compare downstream
CI results on matching tasks and protocols; preserve separate identities when the
configuration differs. Keep runtime success, accuracy acceptance and incomplete
coverage distinct in the final readiness report.
