"""Per-kernel-op summary of op_table.csv: one row per device op and program factory.

Measured counts come from Tracy, which reports op codes but not program factories, so they are
given per op code and repeated on every kernel op that shares it.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import TEST_TARGETS, kernel_ops_columns, read_csv, run_targets, write_csv  # noqa: E402

STATUS_ORDER = ("✅", "⚠️", "❌", "n/a")


def measured_counts(path: pathlib.Path | None, inferences: int = 1) -> dict[str, int]:
    """Launches per inference by op code; rounded down when a count is not a whole multiple."""
    if path is None:
        return {}
    rows = [r for r in read_csv(path) if r["op_type"] == "tt_dnn_device"]
    return {code: n // inferences for code, n in collections.Counter(r["op_code"] for r in rows).items()}


def _tests(cell: str) -> list[str]:
    return [t.strip() for t in cell.split(";") if t.strip()]


def aggregate(rows: list[dict], source: str, destinations: list[str], measured: dict[str, int],
              node_results: dict[str, dict[str, str]] | None = None) -> list[dict]:
    """node_results maps a target to {node id: result} from its kernel_test_results file."""
    node_results = node_results or {}
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["device_op"], row["program_factory"], row["op_code"]), []).append(row)
    static_by_code: collections.Counter = collections.Counter()
    for (_, _, code), members in groups.items():
        static_by_code[code] += sum(int(m[f"{source}:launches"] or 0) for m in members if m["origin"] != "measured")
    out = []
    for (device_op, factory, code), members in groups.items():
        static_rows = [m for m in members if m["origin"] != "measured"]
        entry = {
            "device_op": device_op, "program_factory": factory, "op_code": code,
            "origin": "measured" if not static_rows else
                      ("static+measured" if any(m["origin"] == "static+measured" for m in members) else "static"),
            "op_table_ids": ",".join(m["id"] for m in members),
            "static_launches": str(sum(int(m[f"{source}:launches"] or 0) for m in static_rows)),
            "static_launches_op_code": str(static_by_code[code]),
            "measured_launches_op_code": str(measured.get(code, 0)),
        }
        for t in (source, *destinations):
            statuses = collections.Counter(m[f"{t}:status"] for m in members)
            entry[f"{t}:statuses"] = "; ".join(f"{s} {statuses[s]}" for s in STATUS_ORDER if statuses[s])
            if t in TEST_TARGETS:
                entry[f"{t}:tests"] = "; ".join(sorted({x for m in members for x in _tests(m[f"{t}:tests"])}))
                if t in node_results:
                    # A row's result is the worst of its nodes; each node shows only its own run.
                    results = {f"{x}: {node_results[t][x]}" for m in members for x in _tests(m[f"{t}:tests"])
                               if x in node_results[t]}
                else:
                    results = {f"{x}: {m[f'{t}:test_result']}" for m in members for x in _tests(m[f"{t}:tests"])
                               if m[f"{t}:test_result"] != "not run"}
                entry[f"{t}:test_results"] = "; ".join(sorted(results))
        out.append(entry)
    known = {code for (_, _, code) in groups}
    for code, count in measured.items():
        if code not in known:
            out.append({"device_op": "", "program_factory": "", "op_code": code, "origin": "measured",
                        "op_table_ids": "", "static_launches": "0", "static_launches_op_code": "0",
                        "measured_launches_op_code": str(count)}
                       | {f"{t}:statuses": "" for t in (source, *destinations)})
    for n, entry in enumerate(out, 1):
        entry["id"] = str(n)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("--measured", type=pathlib.Path)
    args = parser.parse_args()
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    rows = read_csv(args.run_dir / "op_table.csv")
    measured = args.measured or (pathlib.Path(run["measured_ops"]) if run.get("measured_ops") else None)
    counts = measured_counts(measured, int(run.get("measured_inferences", 1)))
    source, destinations = run_targets(run)
    node_results = {}
    for t in (source, *destinations):
        path = args.run_dir / f"kernel_test_results_{t}.csv"
        if t in TEST_TARGETS and path.exists():
            node_results[t] = {r["node_id"]: r["result"] for r in read_csv(path)}
    out = aggregate(rows, source, destinations, counts, node_results)
    write_csv(args.run_dir / "kernel_ops.csv", kernel_ops_columns(source, destinations), out)
    print(f"{len(out)} kernel ops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
