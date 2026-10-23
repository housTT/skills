# CI compute for goal 12

Read this when `compute.kind` is `github_actions` in `bringup/agentic_evals/inputs.json`. It covers
the input shape, what has to be published before a run can start, and the dispatch loop.

A CI grant differs from a local device in one way that shapes everything else: the runners do not
share your filesystem. They fetch the implementation from Git refs. So the unit of work is not
"run this script", it is "publish a ref, then dispatch a run that names it".

## Input shape

```json
{
  "compute": {
    "kind": "github_actions",
    "repo": "<owner>/<ci-repo>",
    "workflow": "<workflow-file.yml>",
    "max_parallel": 6,
    "runners": [
      {"runner_label": "<label>", "device_type": "<device>", "count": 6}
    ],
    "dispatch_inputs": {
      "workflow": "agentic",
      "impl-of-model": "default",
      "run-full-evals": false
    },
    "publish": {
      "tt_metal": {
        "remote": "https://github.com/<owner>/tt-metal.git",
        "branch": "bringup/<model-slug>-<run-id>",
        "base": "<pinned upstream launch commit>"
      },
      "tt_inference_server": {
        "remote": "https://github.com/<owner>/tt-inference-server.git",
        "branch": "bringup/<model-slug>-<run-id>",
        "base": "<upstream base ref>"
      }
    }
  }
}
```

`max_parallel` is a ceiling on runs in flight, not a target. Six granted machines mean you may use
up to six; use what the plan needs. A round of six one-change arms is a good reason to fill the
grant. Dispatching six copies of a run whose result you can already predict is not.

`runners` may list several entries when the grant spans device shapes. Prove the harness on each
distinct `runner_label` before scoring on it — a label is a different machine class, not a
duplicate.

`dispatch_inputs` is passed through to the workflow as-is. Do not invent inputs the workflow does
not declare; read its `workflow_dispatch` block and match the names exactly.

## Publish contract

Two refs must exist before the first dispatch.

**tt-metal** carries the implementation. Push *only* the implementation, as **one commit on top of
the pinned upstream base** — not the accumulated stage history, and not the evidence tree. Every
run on every granted machine clones this ref; artifacts committed here are re-downloaded on each
clone, on each machine, for the rest of the stage.

Include `models/autoports/<model>/` and any file outside it the implementation actually imports.
Exclude everything the stages wrote as evidence rather than as code:

- `models/autoports/<model>/doc/` — work logs, READMEs, contracts, per-stage reports
- perf CSVs, tt-perf-report tables, profiler and Tracy dumps
- `runs/`, captured traces, collected eval results, generated weights or caches

Build it in a separate publish checkout. Do not add a remote to the execution checkout and do not
push from it: in a clean-room lane that checkout has no remotes and blocks Git transports on
purpose, and restoring them to publish destroys the isolation the lane's audit asserts. Clone the
base fresh, copy the implementation paths in, commit once, push.

Verify before dispatching — a run that fails on a missing file costs a machine-hour to learn
something `git ls-tree` answers in a second:

```bash
git ls-tree -r --name-only "$BRANCH" -- "models/autoports/$MODEL" | head -50
git ls-tree -r --long "$BRANCH" -- "models/autoports/$MODEL" | sort -k4 -n -r | head -5
```

If the largest blobs are not source files, evidence leaked in. Fix the branch rather than letting
each run pay for it.

**tt-inference-server** carries whatever the agentic harness needs to serve and score the model —
typically a model spec entry pointing at the implementation. Push it to its own branch on the same
one-commit-on-a-base principle. Its evidence exclusions matter less because it is small, but the
same rule costs nothing.

Record both pushed SHAs in `doc/agentic_evals/compute.json`. When the implementation changes
mid-stage, push a new branch or a new commit and record which runs used which SHA; an arm compared
against a result from a different implementation is not a comparison.

Dispatch by **commit SHA, not branch name**, wherever the workflow accepts a ref. A branch moves,
and a run that starts after you push the next arm silently measures the wrong implementation —
the kind of error that produces a plausible number and no error message. The branch is how the
commit reaches the remote; the SHA is what the run should name.

Check which repository the workflow clones before choosing where to push. A dispatch input named
for a ref and not a repository usually means the canonical upstream, in which case a fork is not
reachable and the branch has to go to the upstream repository itself.

## Dispatch loop

```bash
gh workflow run "$WORKFLOW" -R "$REPO" \
  -f model="$MODEL" \
  -f runner-label="$RUNNER_LABEL" \
  -f device-type="$DEVICE_TYPE" \
  -f tt-metal-git-ref="$TT_METAL_SHA" \
  -f inference-server-git-ref="$TIS_SHA" \
  -f workflow=agentic
```

`gh workflow run` does not return the run id. Resolve it by polling for a run newer than the
dispatch whose `run-name` or head matches the arm, and record it before dispatching the next one.
Untracked runs are the usual way a stage loses a machine: it cannot tell whether a slot is busy, so
it either over-dispatches or sits idle. With several explorers dispatching, the ledger is shared
state — register a run at launch, not at collection.

Poll with `gh run list -R "$REPO" --json databaseId,status,conclusion,displayTitle` on an interval
measured in minutes. Count runs in `queued` and `in_progress` against `max_parallel` and dispatch
the next arm when a slot frees.

Collect with `gh run download <id> -R "$REPO"` into `doc/agentic_evals/runs/<arm>/`, alongside the
manifest recording the arm's prediction, the dispatched inputs, the published SHAs and the run id.

A run that fails for an infrastructure reason — runner offline, image pull, a timeout unrelated to
the model — is not a measurement. Record it, re-dispatch it, and keep it out of the scored set. A
run that fails because the model crashed, looped or produced malformed output *is* a measurement,
and belongs in the report.

## What counts as evidence

Scores from CI are reported, not gated, exactly as for local runs. What CI adds to the record is
traceability: `compute.json` ties every score to a run id, a runner label and the implementation SHA
it ran. Without those three, a reader cannot tell whether two numbers are comparable, and the
report's arm-by-arm reasoning is unverifiable.
