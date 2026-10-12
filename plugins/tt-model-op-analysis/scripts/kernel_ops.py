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
from opa_schema import TEST_TARGETS, kernel_ops_columns, read_csv, write_csv  # noqa: E402

STATUS_ORDER = ("✅", "⚠️", "❌", "n/a")


def measured_counts(path: pathlib.Path | None, inferences: int = 1) -> dict[str, int]:
    """Launches per inference by op code; rounded down when a count is not a whole multiple."""
    if path is None:
        return {}
    rows = [r for r in read_csv(path) if r["op_type"] == "tt_dnn_device"]
    return {code: n // inferences for code, n in collections.Counter(r["op_code"] for r in rows).items()}


def _tests(cell: str) -> list[str]:
    return [t.strip() for t in cell.split(";") if t.strip()]


def aggregate(rows: list[dict], source: str, destination: str, measured: dict[str, int]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["device_op"], row["program_factory"], row["op_code"]), []).append(row)
    static_by_code: collections.Counter = collections.Counter()
    for (_, _, code), members in groups.items():
        static_by_code[code] += sum(int(m[f"{source}:launches"] or 0) for m in members if m["origin"] != "measured")
    out = []
    for (device_op, factory, code), members in groups.items():
        static_rows = [m for m in members if m["origin"] != "measured"]
        statuses = collections.Counter(m[f"{destination}:status"] for m in members)
        entry = {
            "device_op": device_op, "program_factory": factory, "op_code": code,
            "origin": "measured" if not static_rows else
                      ("static+measured" if any(m["origin"] == "static+measured" for m in members) else "static"),
            "op_table_ids": ",".join(m["id"] for m in members),
            "static_launches": str(sum(int(m[f"{source}:launches"] or 0) for m in static_rows)),
            "static_launches_op_code": str(static_by_code[code]),
            "measured_launches_op_code": str(measured.get(code, 0)),
            f"{destination}:statuses": "; ".join(f"{s} {statuses[s]}" for s in STATUS_ORDER if statuses[s]),
        }
        if destination in TEST_TARGETS:
            entry[f"{destination}:tests"] = "; ".join(sorted({t for m in members for t in _tests(m[f"{destination}:tests"])}))
            entry[f"{destination}:test_results"] = "; ".join(sorted(
                {f"{t}: {m[f'{destination}:test_result']}" for m in members for t in _tests(m[f"{destination}:tests"])
                 if m[f"{destination}:test_result"] != "not run"}))
        out.append(entry)
    known = {code for (_, _, code) in groups}
    for code, count in measured.items():
        if code not in known:
            out.append({"device_op": "", "program_factory": "", "op_code": code, "origin": "measured",
                        "op_table_ids": "", "static_launches": "0", "static_launches_op_code": "0",
                        "measured_launches_op_code": str(count), f"{destination}:statuses": ""})
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
    out = aggregate(rows, run["source"], run["destination"], counts)
    write_csv(args.run_dir / "kernel_ops.csv", kernel_ops_columns(run["source"], run["destination"]), out)
    print(f"{len(out)} kernel ops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
