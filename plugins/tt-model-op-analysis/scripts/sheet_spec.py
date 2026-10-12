"""Spreadsheet layout for a run: tab order, readable headers and the formatting requests.

Prints JSON with `info` and `summary` (sheetId and title of the first two tabs), `tabs` (one entry
per CSV present in the run: csv, tab title, sheetId, display headers, row count) and `requests` (a
Google Sheets batchUpdate list that renames the first tab to Info, adds Summary as the second tab,
adds the CSV tabs after it and formats every tab: wrapped top-aligned cells, fixed column widths,
bold frozen header row). Merge outputs (`changes_<table>.csv`) become `Changes: <tab>` tabs.
`kernel_ops.csv` is listed in `summary_tables` with its headers, to be written into the Summary tab.

For an existing spreadsheet, delete every tab but the first and clear it, then pass that tab's id
as --info-sheet-id and a --first-sheet-id above every id the spreadsheet has used.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import SHEET_TABS, TARGET_LABELS, column_width, display_header, read_csv, read_header  # noqa: E402

HEADER_GREY = {"red": 0.9, "green": 0.9, "blue": 0.9}
INFO_WIDTHS = (260, 900)
SUMMARY_WIDTHS = (300, 700, 90, 90, 90, 90)


def _wrap_all(sheet_id: int) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id},
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
    }}


def _width(sheet_id: int, index: int, pixels: int) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1},
        "properties": {"pixelSize": pixels}, "fields": "pixelSize",
    }}


def spec(run_dir: pathlib.Path, info_id: int = 0, first_id: int = 1) -> dict:
    tabs = []
    summary_id = first_id
    requests = [{"updateSheetProperties": {"properties": {"sheetId": info_id, "title": "Info"},
                                           "fields": "title"}},
                _wrap_all(info_id)]
    requests += [_width(info_id, i, w) for i, w in enumerate(INFO_WIDTHS)]
    requests.append({"addSheet": {"properties": {"sheetId": summary_id, "title": "Summary", "index": 1}}})
    requests.append(_wrap_all(summary_id))
    requests += [_width(summary_id, i, w) for i, w in enumerate(SUMMARY_WIDTHS)]
    summary_tables = []
    kernel_ops = run_dir / "kernel_ops.csv"
    if kernel_ops.exists():
        columns = read_header(kernel_ops)
        summary_tables.append({"csv": kernel_ops.name, "title": "Kernel ops",
                               "headers": [display_header(c) for c in columns],
                               "rows": len(read_csv(kernel_ops))})
    # The kernel-op table is part of the Summary tab, not a tab of its own.
    sources = []
    for name, title in SHEET_TABS:
        if name == "kernel_ops":
            continue
        if name == "kernel_test_results":
            for path in sorted(run_dir.glob("kernel_test_results_*.csv")):
                target = path.stem.removeprefix("kernel_test_results_")
                sources.append((path.stem, f"Kernel tests ({TARGET_LABELS.get(target, target)})"))
            continue
        sources.append((name, title))
    sources += [(f"changes_{name}", f"Changes: {title}") for name, title in SHEET_TABS]
    for name, title in sources:
        path = run_dir / f"{name}.csv"
        if not path.exists():
            continue
        sheet_id = summary_id + 1 + len(tabs)
        columns = read_header(path)
        rows = len(read_csv(path))
        tabs.append({"csv": path.name, "tab": title, "sheetId": sheet_id,
                     "headers": [display_header(c) for c in columns], "rows": rows})
        requests.append({"addSheet": {"properties": {
            "sheetId": sheet_id, "title": title, "index": len(tabs) + 1,
            "gridProperties": {"rowCount": rows + 50, "columnCount": len(columns), "frozenRowCount": 1},
        }}})
        requests.append(_wrap_all(sheet_id))
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                           "backgroundColorStyle": {"rgbColor": HEADER_GREY}}},
            "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColorStyle",
        }})
        requests += [_width(sheet_id, i, column_width(c)) for i, c in enumerate(columns)]
    return {"info": {"sheetId": info_id, "title": "Info"}, "summary": {"sheetId": summary_id, "title": "Summary"},
            "tabs": tabs, "summary_tables": summary_tables, "requests": requests}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("--info-sheet-id", type=int, default=0)
    parser.add_argument("--first-sheet-id", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(spec(args.run_dir, args.info_sheet_id, args.first_sheet_id), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
