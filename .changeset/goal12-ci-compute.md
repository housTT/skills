---
"tt-model-bringup": patch
---

Let goal 12 dispatch agentic evals to a CI grant instead of only the local device.
The `compute` input gains a `github_actions` form alongside the existing
`submit`/`status`/`collect` commands: a CI repository, a dispatch workflow, runner
labels with their device types, and pass-through dispatch inputs.

CI runners do not share the stage's filesystem, so a grant also carries a publish
contract. The implementation goes to a tt-metal branch as one commit on the pinned
upstream base, carrying `models/autoports/<model>/` source only — evidence trees,
perf CSVs, profiler dumps and collected results are excluded, because every run on
every granted machine clones that ref and would otherwise re-download them. The
agentic harness's model spec goes to a tt-inference-server branch on the same basis.
Publishing happens in a separate checkout: the execution checkout is never given a
remote, which keeps a clean-room lane's Git isolation intact.

Adds `references/ci-compute.md` with the input shape, the publish contract and its
verification, the dispatch and polling loop, and the rule that infrastructure
failures are re-dispatched while model failures are measurements. `max_parallel` is
documented as a ceiling to fill when the plan needs it, not a quota to spend.
Stage 12 records the grant as used in `doc/agentic_evals/compute.json` — published
SHAs, runner labels and every dispatched run id — so each score traces to a run and
two numbers can be known to be comparable. The stage prompt's blanket "never push"
now scopes to the execution checkout.
