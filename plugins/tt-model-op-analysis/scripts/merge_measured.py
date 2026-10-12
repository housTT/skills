"""Merge a Tracy measured_ops.csv of the entry test into op_table.csv.

Static rows whose op code appears in the measured run become `static+measured`. Op codes that only
the measured run shows become `measured` rows, inserted after the static row that ran just before
their first launch, so the table stays in execution order. Launch counts are divided by the number
of inferences in the measured window. Re-running replaces earlier measured rows.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import (  # noqa: E402
    LAUNCH_PROFILES, NOT_TRACED, TEST_TARGETS, op_table_columns, read_csv, run_targets, write_csv,
)


def _measured_row(code: str, count: int, first: dict, source: str, destinations: list[str], csv_name: str) -> dict:
    row = {
        "stage": f"measured only {code}", "origin": "measured", "ttnn_api": "", "op_code": code,
        "device_op": "", "program_factory": "", "call_site": NOT_TRACED, "launch_condition": "always",
        "confidence": "unverified",
        "dispatch_evidence": f"seen in {csv_name} (first at measured row {first['id']}); not traced statically",
        "grid_dependency": NOT_TRACED,
        f"{source}:shapes": f"{first['inputs']} -> {first['outputs']}", f"{source}:launches": str(count),
        f"{source}:status": "✅", f"{source}:evidence": f"executed by the entry test on {source} ({csv_name})",
    }
    for target in destinations:
        if target in LAUNCH_PROFILES:
            row[f"{target}:shapes"] = NOT_TRACED
            row[f"{target}:launches"] = ""
        row[f"{target}:status"] = "⚠️"
        row[f"{target}:evidence"] = "measured-only op; kernel-op lookup on this target still needed"
    for target in (source, *destinations):
        if target in TEST_TARGETS:
            row[f"{target}:tests"] = ""
            row[f"{target}:test_result"] = "not run"
    return row


def merge(rows: list[dict], measured: list[dict], source: str, destinations: list[str], csv_name: str,
          inferences: int = 1) -> list[dict]:
    rows = [dict(r, origin="static") for r in rows if r["origin"] != "measured"]
    device = [m for m in measured if m["op_type"] == "tt_dnn_device"]
    counts = collections.Counter(m["op_code"] for m in device)
    static_codes = {r["op_code"] for r in rows}
    for row in rows:
        if row["op_code"] in counts:
            row["origin"] = "static+measured"
    bad = {c: n for c, n in counts.items() if c not in static_codes and n % inferences}
    if bad:
        raise ValueError(f"launch counts not divisible by {inferences} inferences: {bad}")
    # Walk the measured stream with a cursor over the static rows. A row can cover several repeated
    # modules, so the stream may return to an earlier row; the cursor only moves forward.
    first_seen: dict[str, dict] = {}
    insert_after: dict[str, int] = {}
    cursor = -1
    for m in device:
        code = m["op_code"]
        if code in static_codes:
            ahead = next((i for i in range(max(cursor, 0), len(rows)) if rows[i]["op_code"] == code), None)
            if ahead is not None:
                cursor = ahead
        elif code not in first_seen:
            first_seen[code] = m
            insert_after[code] = cursor

    def new_rows(after: int) -> list[dict]:
        return [_measured_row(c, counts[c] // inferences, first_seen[c], source, destinations, csv_name)
                for c in first_seen if insert_after[c] == after]

    out = new_rows(-1)
    for i, row in enumerate(rows):
        out.append(row)
        out += new_rows(i)
    for n, row in enumerate(out, 1):
        row["id"] = str(n)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("measured_ops", type=pathlib.Path)
    parser.add_argument("--inferences", type=int, default=1,
                        help="forward passes inside the measured window (1 for a warm signposted window)")
    args = parser.parse_args()
    run_path = args.run_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    op_path = args.run_dir / "op_table.csv"
    source, destinations = run_targets(run)
    try:
        merged = merge(read_csv(op_path), read_csv(args.measured_ops), source, destinations,
                       args.measured_ops.name, args.inferences)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    write_csv(op_path, op_table_columns(source, destinations), merged)
    run["measured_ops"] = str(args.measured_ops)
    run["measured_inferences"] = args.inferences
    run_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(f"{len(merged)} rows, {sum(r['origin'] == 'measured' for r in merged)} measured-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
