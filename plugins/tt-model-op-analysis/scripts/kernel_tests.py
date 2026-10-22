"""Run a Quasar target's kernel-op tests on TTsim and apply the results to op_table.csv.

The target is a Quasar source or destination (--target, implied when the run has only one); its
files are kernel_tests_plan_<target>.csv and kernel_test_results_<target>.csv.

plan:  collect unique pytest node ids from <target>:tests, with the statuses of the rows that cite
       them. Only kernel-op tests: an entry test never runs on TTsim.
check: collect the plan's test files with pytest and list the node ids pytest does not find.
run:   run each node with plain pytest under the run's TTsim environment, one process per node.
       Refuses a checkout that is not the run's commit, and refuses to start while a selected node
       id is not collected. pytest-timeout is disabled and there is no wall-clock limit unless
       --timeout is given (references/ttsim.md); a --timeout kill is recorded as `timeout`. Nodes
       with a pass, fail, xfail or skip are skipped unless --rerun; error and timeout nodes run
       again. --only and --env-json run one batch of nodes under its own environment and keep the
       other batches' results. --status runs the nodes of rows with that status.
apply: set <target>:test_result and status per op_table row. A fail or xfail of any of its tests
       gives ❌; ✅ needs every test to pass; a timeout, an error or an unrun test decides nothing.

Exit codes: 1 checkout is not the run's commit, 2 no TTsim environment, 3 a node is not collected.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import (  # noqa: E402
    EVIDENCE_SEPARATOR, FAILING_RESULTS, TEST_TARGETS, TTSIM_PREFIX, UNDECIDED_RESULTS, op_table_columns,
    read_csv, read_results, run_targets, split_evidence, write_csv,
)
from same_commit import mismatch  # noqa: E402

KERNEL_TEST_COLUMNS = ("id", "node_id", "op_table_ids", "result", "duration_s", "log", "environment")
PLAN_COLUMNS = ("id", "node_id", "op_table_ids", "row_statuses")
SEVERITY = (*FAILING_RESULTS, *UNDECIDED_RESULTS, "pass", "skip", "not run")
# Wall-clock limits are meaningless on TTsim; the pinned tt-metal pytest.ini sets one.
NO_PYTEST_TIMEOUT = ("-p", "no:timeout")
EXIT_COMMIT, EXIT_ENVIRONMENT, EXIT_NOT_COLLECTED = 1, 2, 3


def _run_json(run_dir: pathlib.Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _target(run: dict, requested: str | None) -> str:
    source, destinations = run_targets(run)
    candidates = [t for t in (source, *destinations) if t in TEST_TARGETS]
    if requested:
        if requested not in candidates:
            raise SystemExit(f"ERROR: target '{requested}' has no kernel-op tests in this run ({candidates})")
        return requested
    if len(candidates) != 1:
        raise SystemExit(f"ERROR: pass --target, one of {candidates}")
    return candidates[0]


def _plan_path(run_dir: pathlib.Path, target: str) -> pathlib.Path:
    return run_dir / f"kernel_tests_plan_{target}.csv"


def _results_path(run_dir: pathlib.Path, target: str) -> pathlib.Path:
    return run_dir / f"kernel_test_results_{target}.csv"


def _tests(cell: str) -> list[str]:
    return [t.strip() for t in cell.split(";") if t.strip()]


def plan_rows(run_info: dict, rows: list[dict], target: str) -> list[dict]:
    """Unique test nodes of a target in op_table order."""
    order: dict[str, dict] = {}
    for row in rows:
        for node in _tests(row[f"{target}:tests"]):
            item = order.setdefault(node, {"ids": [], "statuses": set()})
            item["ids"].append(row["id"])
            item["statuses"].add(row[f"{target}:status"])
    return [{"id": str(n), "node_id": node, "op_table_ids": ",".join(v["ids"]),
             "row_statuses": ";".join(sorted(v["statuses"]))}
            for n, (node, v) in enumerate(order.items(), 1)]


def read_plan(path: pathlib.Path) -> list[dict]:
    # A plan from an earlier plugin version may list the entry test, which cites no row; it never runs on TTsim.
    return [item for item in read_csv(path) if item["op_table_ids"]]


def plan(run_dir: pathlib.Path, target: str | None) -> int:
    run_info = _run_json(run_dir)
    target = _target(run_info, target)
    out = plan_rows(run_info, read_csv(run_dir / "op_table.csv"), target)
    write_csv(_plan_path(run_dir, target), PLAN_COLUMNS, out)
    print(f"{len(out)} tests to run for {target}")
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


def _environment(run_dir: pathlib.Path, env_json: pathlib.Path | None) -> tuple[dict, str] | None:
    if env_json is not None:
        env_extra, env_name = json.loads(env_json.read_text(encoding="utf-8")), env_json.name
    else:
        env_extra, env_name = _run_json(run_dir).get("ttsim_environment"), "run.json"
    if not isinstance(env_extra, dict):
        print("ERROR: run.json has no ttsim_environment; set it per references/ttsim.md")
        return None
    return dict(os.environ, **{k: str(v) for k, v in env_extra.items()}), env_name


def _collect(file: str, pytest_cmd: str, tt_metal: pathlib.Path, env: dict) -> tuple[set[str], str]:
    with tempfile.TemporaryDirectory() as tmp:
        # --verbosity overrides the -v in a repo's addopts, which -q only offsets; below 0 prints
        # node ids. A private --junitxml keeps parallel collections off the report path in addopts.
        cmd = shlex.split(pytest_cmd) + [*NO_PYTEST_TIMEOUT, "--collect-only", "--verbosity=-1",
                                         f"--junitxml={pathlib.Path(tmp) / 'collect.xml'}", file]
        out = subprocess.run(cmd, cwd=tt_metal, env=env, capture_output=True, text=True)
    text = out.stdout + out.stderr
    return {line.strip() for line in text.splitlines() if line.startswith(f"{file}::")}, text


def _selects(node: str, collected: set[str]) -> bool:
    """A node id names collected items: itself, or a function or class that holds them."""
    return any(c == node or c.startswith((f"{node}[", f"{node}::")) for c in collected)


def _collection(nodes: list[str], pytest_cmd: str, tt_metal: pathlib.Path,
                env: dict) -> tuple[dict[str, tuple[set[str], str]], list[str]]:
    """Collect each test file once and return its output plus missing plan nodes."""
    files = sorted({n.split("::", 1)[0] for n in nodes})
    if not files:
        return {}, []
    with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
        found = dict(zip(files, pool.map(lambda f: _collect(f, pytest_cmd, tt_metal, env), files)))
    missing = [n for n in nodes if not _selects(n, found[n.split("::", 1)[0]][0])]
    return found, missing


def _same_function(node: str, collected: set[str]) -> list[str]:
    return sorted(c for c in collected if c.split("[", 1)[0] == node.split("[", 1)[0])


def _print_missing(missing: list[str], found: dict[str, tuple[set[str], str]]) -> None:
    for node in missing:
        collected, text = found[node.split("::", 1)[0]]
        same_function = _same_function(node, collected)
        print(f"NOT COLLECTED: {node}")
        if same_function:
            print(f"  collected: {', '.join(same_function)}")
        elif collected:
            print("  collected: none of this function")
        else:
            print("  the file collected nothing:\n" + "\n".join(text.splitlines()[-10:]))


def _added_prefix_match(node: str, collected: set[str]) -> str | None:
    """Return one collected ID that only prefixes the requested parametrization."""
    if "[" not in node or not node.endswith("]"):
        return None
    function, parameter = node.rsplit("[", 1)
    parameter = parameter[:-1]
    matches = []
    for candidate in _same_function(node, collected):
        candidate_parameter = candidate.rsplit("[", 1)[1][:-1]
        if candidate.startswith(f"{function}[") and candidate_parameter.endswith(f"-{parameter}"):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _fix_node_ids(run_dir: pathlib.Path, target: str, missing: list[str],
                  found: dict[str, tuple[set[str], str]]) -> dict[str, str]:
    replacements = {}
    for node in missing:
        replacement = _added_prefix_match(node, found[node.split("::", 1)[0]][0])
        if replacement:
            replacements[node] = replacement
    if not replacements:
        return replacements
    run_info = _run_json(run_dir)
    rows = read_csv(run_dir / "op_table.csv")
    column = f"{target}:tests"
    for row in rows:
        row[column] = "; ".join(replacements.get(node, node) for node in _tests(row[column]))
    write_csv(run_dir / "op_table.csv", op_table_columns(*run_targets(run_info)), rows)
    plan(run_dir, target)
    for old, new in replacements.items():
        print(f"UPDATED: {old} -> {new}")
    return replacements


def not_collected(nodes: list[str], pytest_cmd: str, tt_metal: pathlib.Path, env: dict) -> int:
    """Print the nodes pytest does not collect, with the collected ids of the same test function.

    Each file is collected in its own process, as `run` executes it: a conftest hook such as
    pytest_make_parametrize_id renames the ids of every file in a session once it is loaded.
    Collection imports ttnn but starts no simulator, so files are collected in parallel.
    """
    found, missing = _collection(nodes, pytest_cmd, tt_metal, env)
    _print_missing(missing, found)
    return len(missing)


def check(run_dir: pathlib.Path, target: str | None, tt_metal: pathlib.Path, pytest_cmd: str | None,
          env_json: pathlib.Path | None, fix_node_ids: bool = False) -> int:
    target = _target(_run_json(run_dir), target)
    problem = mismatch(run_dir, tt_metal)
    if problem:
        print(f"ERROR: {problem}")
        return EXIT_COMMIT
    environment = _environment(run_dir, env_json)
    if environment is None:
        return EXIT_ENVIRONMENT
    nodes = [p["node_id"] for p in read_plan(_plan_path(run_dir, target))]
    found, missing_nodes = _collection(nodes, pytest_cmd or _default_pytest(tt_metal), tt_metal, environment[0])
    if fix_node_ids and missing_nodes:
        replacements = _fix_node_ids(run_dir, target, missing_nodes, found)
        nodes = [replacements.get(node, node) for node in nodes]
        missing_nodes = [node for node in nodes if not _selects(node, found[node.split("::", 1)[0]][0])]
    _print_missing(missing_nodes, found)
    missing = len(missing_nodes)
    print(f"{len(nodes) - missing} of {len(nodes)} plan nodes collected")
    if missing:
        print(f"Fix them in op_table.csv {target}:tests (or the script that writes it), then rerun plan and check")
    return EXIT_NOT_COLLECTED if missing else 0


def run(run_dir: pathlib.Path, target: str | None, tt_metal: pathlib.Path, timeout: int | None,
        pytest_cmd: str | None, rerun: bool, only: str | None, env_json: pathlib.Path | None,
        status: str | None = None) -> int:
    target = _target(_run_json(run_dir), target)
    problem = mismatch(run_dir, tt_metal)
    if problem:
        print(f"ERROR: {problem}")
        return EXIT_COMMIT
    environment = _environment(run_dir, env_json)
    if environment is None:
        return EXIT_ENVIRONMENT
    env, env_name = environment
    pytest_cmd = pytest_cmd or _default_pytest(tt_metal)
    results_path = _results_path(run_dir, target)
    kept = {r["node_id"]: r for r in read_results(results_path)} if results_path.exists() else {}
    planned = read_plan(_plan_path(run_dir, target))
    selected = [item for item in planned
                if (not only or only in item["node_id"])
                and (not status or status in item["row_statuses"].split(";"))]
    todo = [item for item in selected if rerun or item["node_id"] not in kept
            or kept[item["node_id"]]["result"] in UNDECIDED_RESULTS]
    if not_collected([item["node_id"] for item in todo], pytest_cmd, tt_metal, env):
        print(f"ERROR: fix these ids in op_table.csv {target}:tests (or the script that writes it),"
              " then rerun kernel_tests.py plan")
        return EXIT_NOT_COLLECTED
    log_dir = run_dir / "kernel_tests"
    log_dir.mkdir(exist_ok=True)
    ran, skipped = 0, len(selected) - len(todo)
    for item in todo:
        log, xml = log_dir / f"{item['id']}.log", log_dir / f"{item['id']}.xml"
        xml.unlink(missing_ok=True)
        cmd = shlex.split(pytest_cmd) + [*NO_PYTEST_TIMEOUT, "-q", "-rA", f"--junitxml={xml}", item["node_id"]]
        start = time.monotonic()
        with log.open("w", encoding="utf-8") as fh:
            try:
                subprocess.run(cmd, cwd=tt_metal, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
                result = _junit_result(xml)
            except subprocess.TimeoutExpired:
                result = "timeout"
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
    failing = [r for r in results if r in FAILING_RESULTS]
    if failing:
        return min(failing, key=SEVERITY.index)
    if results and all(r == "pass" for r in results):
        return "pass"
    undecided = [r for r in results if r in UNDECIDED_RESULTS]
    if undecided:
        return min(undecided, key=SEVERITY.index)
    return "skip" if "skip" in results and "not run" not in results else "not run"


def apply(run_dir: pathlib.Path, target: str | None) -> int:
    run_info = _run_json(run_dir)
    target = _target(run_info, target)
    results = {r["node_id"]: r["result"] for r in read_results(_results_path(run_dir, target))}
    rows = read_csv(run_dir / "op_table.csv")
    for row in rows:
        nodes = _tests(row[f"{target}:tests"])
        if not nodes:
            continue
        outcome = _row_result([results.get(n, "not run") for n in nodes])
        row[f"{target}:test_result"] = outcome
        if outcome == "pass" or outcome in FAILING_RESULTS:
            row[f"{target}:status"] = "✅" if outcome == "pass" else "❌"
            summary = "; ".join(f"{n} {results[n]}" for n in nodes if n in results)
            static = split_evidence(row[f"{target}:evidence"])[1]
            row[f"{target}:evidence"] = f"{TTSIM_PREFIX}{summary}{EVIDENCE_SEPARATOR}{static}"
        elif row[f"{target}:evidence"].startswith(TTSIM_PREFIX):
            print(f"WARNING: row {row['id']}: {target}:status rests on an earlier TTsim result that the"
                  " results file no longer decides; withdraw it (measured-destination.md)")
    write_csv(run_dir / "op_table.csv", op_table_columns(*run_targets(run_info)), rows)
    print(f"applied {len(results)} results")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_check = sub.add_parser("check")
    p_run = sub.add_parser("run")
    p_apply = sub.add_parser("apply")
    for p in (p_plan, p_check, p_run, p_apply):
        p.add_argument("run_dir", type=pathlib.Path)
        p.add_argument("--target", help="Quasar source or destination; implied when the run has only one")
    for p in (p_check, p_run):
        p.add_argument("--tt-metal", type=pathlib.Path, required=True)
        p.add_argument("--pytest-cmd", help="default: <tt-metal>/python_env/bin/python -m pytest when present")
        p.add_argument("--env-json", type=pathlib.Path, help="environment for this batch instead of run.json")
    p_check.add_argument("--fix-node-ids", action="store_true",
                         help="repair one unambiguous prefix added to a parametrized pytest id")
    p_run.add_argument("--timeout", type=int, help="seconds per node; default: no limit")
    p_run.add_argument("--rerun", action="store_true")
    p_run.add_argument("--only", help="run only plan nodes whose id contains this text")
    p_run.add_argument("--status", choices=("✅", "⚠️", "❌"), help="run only nodes cited by rows with this status")
    args = parser.parse_args()
    if args.cmd == "plan":
        return plan(args.run_dir, args.target)
    if args.cmd == "check":
        return check(args.run_dir, args.target, args.tt_metal, args.pytest_cmd, args.env_json, args.fix_node_ids)
    if args.cmd == "run":
        return run(args.run_dir, args.target, args.tt_metal, args.timeout, args.pytest_cmd, args.rerun, args.only,
                   args.env_json, args.status)
    return apply(args.run_dir, args.target)


if __name__ == "__main__":
    sys.exit(main())
