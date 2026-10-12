"""Add the origin and kernel-op test columns to an op_table.csv written before they existed."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import TEST_TARGETS, op_table_columns, read_csv, write_csv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    args = parser.parse_args()
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    if "source" not in run:
        print("ERROR: run.json predates source/destination targets (plugin 0.2.0); its columns cannot be "
              "mapped; start a new static run")
        return 2
    path = args.run_dir / "op_table.csv"
    rows = read_csv(path)
    for row in rows:
        row.setdefault("origin", "static")
        for target in (run["source"], run["destination"]):
            if target in TEST_TARGETS:
                row.setdefault(f"{target}:tests", "")
                row.setdefault(f"{target}:test_result", "not run")
    write_csv(path, op_table_columns(run["source"], run["destination"]), rows)
    print(f"upgraded {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
