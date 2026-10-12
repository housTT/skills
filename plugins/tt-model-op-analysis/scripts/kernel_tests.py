"""Run the destination's kernel-op tests on TTsim and apply the results to op_table.csv.

plan:  collect unique pytest node ids from <destination>:tests into kernel_tests_plan.csv.
run:   run each node with plain pytest under the run's TTsim environment, one process per node,
       with a timeout; a timeout is recorded as a hang. Refuses a checkout that is not the run's
       commit. Finished nodes are skipped unless --rerun; --only and --env-json run one batch of
       nodes under its own environment and keep the other batches' results.
apply: set <destination>:test_result and status per op_table row. A fail, hang or xfail of any of
       its tests gives ❌; ✅ needs every test to pass; an error or an unrun test decides nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import TEST_TARGETS, op_table_columns, read_csv, write_csv  # noqa: E402
from same_commit import mismatch  # noqa: E402

KERNEL_TEST_COLUMNS = ("id", "node_id", "op_table_ids", "result", "duration_s", "log", "environment")
PLAN_COLUMNS = ("id", "node_id", "op_table_ids")
SEVERITY = ("hang", "error", "fail", "xfail", "pass", "skip", "not run")
FAILING = ("hang", "fail", "xfail")
# Separates the TTsim result from the static evidence, so apply can be repeated.
EVIDENCE_SEPARATOR = " || "


def _run_json(run_dir: pathlib.Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _destination(run: dict) -> str:
    destination = run["destination"]
    if destination not in TEST_TARGETS:
        raise SystemExit(f"ERROR: destination '{destination}' has no kernel-op tests (only {TEST_TARGETS})")
    return destination


def _tests(cell: str) -> list[str]:
    return [t.strip() for t in cell.split(";") if t.strip()]


def plan(run_dir: pathlib.Path) -> int:
    destination = _destination(_run_json(run_dir))
    order: dict[str, list[str]] = {}
    for row in read_csv(run_dir / "op_table.csv"):
        for node in _tests(row[f"{destination}:tests"]):
            order.setdefault(node, []).append(row["id"])
    out = [{"id": str(n), "node_id": node, "op_table_ids": ",".join(ids)}
           for n, (node, ids) in enumerate(order.items(), 1)]
    write_csv(run_dir / "kernel_tests_plan.csv", PLAN_COLUMNS, out)
    print(f"{len(out)} kernel-op tests to run")
    return 0


def _junit_result(path: pathlib.Path) -> str:
    if not path.exists():
        return "error"
    results = []
    for case in ET.parse(path).getroot().iter("testcase"):
        if case.find("failure") is not None:
            results.append("fail")
        elif case.find("error") is not None:
            results.append("error")
        elif (skipped := case.find("skipped")) is not None:
            results.append("xfail" if "xfail" in (skipped.get("type") or "") else "skip")
        else:
            results.append("pass")
    return min(results, key=SEVERITY.index) if results else "error"


def _default_pytest(tt_metal: pathlib.Path) -> str:
    venv = tt_metal / "python_env" / "bin" / "python"
    return f"{venv if venv.exists() else sys.executable} -m pytest"


def run(run_dir: pathlib.Path, tt_metal: pathlib.Path, timeout: int, pytest_cmd: str | None, rerun: bool,
        only: str | None, env_json: pathlib.Path | None) -> int:
    problem = mismatch(run_dir, tt_metal)
    if problem:
        print(f"ERROR: {problem}")
        return 1
    if env_json is not None:
        env_extra, env_name = json.loads(env_json.read_text(encoding="utf-8")), env_json.name
    else:
        env_extra, env_name = _run_json(run_dir).get("ttsim_environment"), "run.json"
    if not isinstance(env_extra, dict):
        print("ERROR: run.json has no ttsim_environment; set it per references/ttsim.md")
        return 2
    pytest_cmd = pytest_cmd or _default_pytest(tt_metal)
    results_path = run_dir / "kernel_test_results.csv"
    kept = {r["node_id"]: r for r in read_csv(results_path)} if results_path.exists() else {}
    log_dir = run_dir / "kernel_tests"
    log_dir.mkdir(exist_ok=True)
    env = dict(os.environ, **{k: str(v) for k, v in env_extra.items()})
    planned = read_csv(run_dir / "kernel_tests_plan.csv")
    ran = skipped = 0
    for item in planned:
        if only and only not in item["node_id"]:
            continue
        if item["node_id"] in kept and not rerun:
            skipped += 1
            continue
        log, xml = log_dir / f"{item['id']}.log", log_dir / f"{item['id']}.xml"
        xml.unlink(missing_ok=True)
        cmd = shlex.split(pytest_cmd) + ["-q", "-rA", f"--junitxml={xml}", item["node_id"]]
        start = time.monotonic()
        with log.open("w", encoding="utf-8") as fh:
            try:
                subprocess.run(cmd, cwd=tt_metal, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
                result = _junit_result(xml)
            except subprocess.TimeoutExpired:
                result = "hang"
        ran += 1
        kept[item["node_id"]] = {"id": item["id"], "node_id": item["node_id"], "op_table_ids": item["op_table_ids"],
                                 "result": result, "duration_s": f"{time.monotonic() - start:.1f}",
                                 "log": str(log), "environment": env_name}
        write_csv(results_path, KERNEL_TEST_COLUMNS, [kept[p["node_id"]] for p in planned if p["node_id"] in kept])
    write_csv(results_path, KERNEL_TEST_COLUMNS, [kept[p["node_id"]] for p in planned if p["node_id"] in kept])
    run_info = _run_json(run_dir)
    run_info["ttsim_pytest_cmd"] = pytest_cmd
    (run_dir / "run.json").write_text(json.dumps(run_info, indent=2) + "\n", encoding="utf-8")
    print(f"{skipped} already done, {ran} run")
    return 0


def _row_result(results: list[str]) -> str:
    """Failures decide; a pass needs every test to have passed; anything else leaves the row open."""
    failing = [r for r in results if r in FAILING]
    if failing:
        return min(failing, key=SEVERITY.index)
    if results and all(r == "pass" for r in results):
        return "pass"
    return "error" if "error" in results else ("skip" if "skip" in results and "not run" not in results else "not run")


def apply(run_dir: pathlib.Path) -> int:
    run_info = _run_json(run_dir)
    destination = _destination(run_info)
    results = {r["node_id"]: r["result"] for r in read_csv(run_dir / "kernel_test_results.csv")}
    rows = read_csv(run_dir / "op_table.csv")
    for row in rows:
        nodes = _tests(row[f"{destination}:tests"])
        if not nodes:
            continue
        outcome = _row_result([results.get(n, "not run") for n in nodes])
        row[f"{destination}:test_result"] = outcome
        if outcome == "pass" or outcome in FAILING:
            row[f"{destination}:status"] = "✅" if outcome == "pass" else "❌"
            summary = "; ".join(f"{n} {results[n]}" for n in nodes if n in results)
            static = row[f"{destination}:evidence"].split(EVIDENCE_SEPARATOR, 1)[-1]
            row[f"{destination}:evidence"] = f"TTsim run: {summary}{EVIDENCE_SEPARATOR}{static}"
    write_csv(run_dir / "op_table.csv", op_table_columns(run_info["source"], destination), rows)
    print(f"applied {len(results)} results")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").add_argument("run_dir", type=pathlib.Path)
    p_run = sub.add_parser("run")
    p_run.add_argument("run_dir", type=pathlib.Path)
    p_run.add_argument("--tt-metal", type=pathlib.Path, required=True)
    p_run.add_argument("--timeout", type=int, default=900)
    p_run.add_argument("--pytest-cmd", help="default: <tt-metal>/python_env/bin/python -m pytest when present")
    p_run.add_argument("--rerun", action="store_true")
    p_run.add_argument("--only", help="run only plan nodes whose id contains this text")
    p_run.add_argument("--env-json", type=pathlib.Path, help="environment for this batch instead of run.json")
    sub.add_parser("apply").add_argument("run_dir", type=pathlib.Path)
    args = parser.parse_args()
    if args.cmd == "plan":
        return plan(args.run_dir)
    if args.cmd == "run":
        return run(args.run_dir, args.tt_metal, args.timeout, args.pytest_cmd, args.rerun, args.only, args.env_json)
    return apply(args.run_dir)


if __name__ == "__main__":
    sys.exit(main())
