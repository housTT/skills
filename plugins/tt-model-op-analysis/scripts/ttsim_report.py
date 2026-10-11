#!/usr/bin/env python3
"""Export executed TTNN operations from a TTsim graph-report database."""

import argparse
import csv
import json
import shutil
import sqlite3
from collections import OrderedDict
from pathlib import Path


def _write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    if not args.database.is_file():
        raise SystemExit(f"Graph-report database not found: {args.database}")

    with sqlite3.connect(args.database) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        if table is None:
            raise SystemExit(f"No operations table in graph-report database: {args.database}")
        operations = connection.execute(
            "SELECT operation_id, name, rank FROM operations ORDER BY rank, operation_id"
        ).fetchall()

    if not operations:
        raise SystemExit(f"No executed operations recorded in: {args.database}")

    args.run_dir.mkdir(parents=True, exist_ok=True)
    sequence_rows = []
    counts = OrderedDict()
    for index, (operation_id, name, rank) in enumerate(operations, start=1):
        sequence_rows.append(
            {
                "id": index,
                "rank": rank,
                "operation_id": operation_id,
                "operation": name,
            }
        )
        counts[name] = counts.get(name, 0) + 1

    count_rows = [
        {"id": index, "operation": name, "count": count}
        for index, (name, count) in enumerate(counts.items(), start=1)
    ]
    _write_csv(
        args.run_dir / "simulated_ops.csv",
        ["id", "rank", "operation_id", "operation"],
        sequence_rows,
    )
    _write_csv(
        args.run_dir / "simulated_op_counts.csv",
        ["id", "operation", "count"],
        count_rows,
    )

    copied_database = args.run_dir / "ttsim_graph.sqlite"
    if args.database.resolve() != copied_database.resolve():
        shutil.copy2(args.database, copied_database)

    print(
        json.dumps(
            {
                "operation_occurrences": len(sequence_rows),
                "unique_operations": len(count_rows),
                "sequence_csv": str(args.run_dir / "simulated_ops.csv"),
                "counts_csv": str(args.run_dir / "simulated_op_counts.csv"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
