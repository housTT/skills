#!/usr/bin/env python3
"""Check agentic-serving evidence for one bringup stage (see the agentic-serving skill).

Critical (2) only when evidence is missing, or a measured shortfall has no recorded reason.
A shortfall with a `reason` is advisory (1), so a hard problem never blocks later stages;
goal 12 measures what it costs. The runner gives each failure one remediation goal.
Exit 0 pass, 1 advisory, 2 critical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVIDENCE = Path("doc/agentic_serving.json")
EVALS = Path("doc/agentic_evals")
MIN_HIT_FRACTION = 0.9


def shortfall(problems: list, reason, message: str) -> None:
    problems.append((1, f"{message}: {reason}") if reason else (2, f"{message}; fix it or record a reason"))


def positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def read(path: Path, problems: list) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        problems.append((2, f"missing or invalid {path}: {exc}"))
        return {}


def check_memory(evidence: dict, problems: list) -> None:
    workload, ledger = evidence.get("workload") or {}, evidence.get("ledger") or {}
    if not (positive_int(workload.get("target_concurrency")) and positive_int(workload.get("working_set_tokens"))):
        problems.append((2, "workload needs target_concurrency and working_set_tokens"))
        return
    for entry in ledger.get("weights") or [{}]:
        if not entry.get("name"):
            problems.append((2, "ledger.weights must list every weight family"))
        elif entry.get("copies", 1) > 1:
            shortfall(problems, entry.get("copy_reason"), f"weight family {entry['name']!r} is held more than once")
    pool, need = ledger.get("pool_tokens"), workload["target_concurrency"] * workload["working_set_tokens"]
    if not positive_int(pool):
        problems.append((2, "ledger.pool_tokens is missing"))
    elif pool < need:
        shortfall(problems, ledger.get("pool_shortfall_reason"), f"KV pool {pool} < concurrency x working set {need}")


def check_serving(evidence: dict, problems: list) -> None:
    serving = evidence.get("serving") or {}
    if serving.get("prefix_caching") is False:
        shortfall(problems, serving.get("reason"), "prefix caching is disabled for serving")
    elif serving.get("prefix_caching") is not True:
        problems.append((2, "serving.prefix_caching must be recorded"))
    else:
        fraction = serving.get("hit_fraction_of_ceiling")
        if not isinstance(fraction, (int, float)):
            problems.append((2, "serving.hit_fraction_of_ceiling is missing; replay multi-turn traffic"))
        elif fraction < MIN_HIT_FRACTION:
            reason = serving.get("reason") or ("live conversations exceeded the pool" if serving.get("pool_bound") else None)
            shortfall(problems, reason, f"cache reuse is {fraction:.0%} of the reusable ceiling")
    traces = serving.get("trace_captures_after_warmup")
    if not isinstance(traces, dict) or not traces:
        problems.append((2, "serving.trace_captures_after_warmup is missing"))
    elif any(traces.values()):
        shortfall(problems, serving.get("trace_reason"), f"traces rebuilt per request after warm-up: {traces}")


def check_evals(model_dir: Path, problems: list) -> None:
    evals = model_dir / EVALS
    for name in ("plan.json", "serving_config.json", "REPORT.md"):
        if not (evals / name).is_file():
            problems.append((2, f"missing {evals / name}"))
    decisions = read(evals / "estimate.json", problems).get("evals") or {}
    if not decisions:
        problems.append((2, "estimate.json has no per-eval decisions"))
    for name, row in decisions.items():
        if row.get("decision") not in ("go", "stop") or not row.get("reason"):
            problems.append((2, f"{name}: estimate needs decision go/stop and a reason"))
        elif row["decision"] == "stop":
            problems.append((1, f"{name}: full suite not run: {row['reason']}"))
        elif not isinstance(read(evals / "full" / name / "results.json", problems).get("actual_hours"), (int, float)):
            problems.append((2, f"{name}: full results lack actual_hours"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    parser.add_argument("--stage", required=True, choices=("full-model", "vllm", "optimized-vllm", "agentic-evals"))
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    problems: list = []
    evidence = read(model_dir / EVIDENCE, problems)
    if evidence:
        check_memory(evidence, problems)
        if args.stage != "full-model":
            check_serving(evidence, problems)
        if args.stage == "agentic-evals":
            check_evals(model_dir, problems)
    for level, message in problems:
        print(f"{'CRITICAL' if level == 2 else 'ADVISORY'}: {message}", file=sys.stderr)
    return max((level for level, _ in problems), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
