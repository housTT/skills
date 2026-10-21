"""Spreadsheet view of a run: tab layout, formatting requests and the values of every tab.

The sheet is a view of the run's files for human readers. Nothing is invented: the view drops
op-table columns that hold one value in every row (listed once on Info), merges device op and
program factory, shortens source paths to their last two components, strips row-number prefixes
from evidence and orders columns reader-first. Tabs: Info (run, counts, summary.md, constant
columns), Kernel ops (one row per kernel op), Op table (one row per launching variant), then the
other CSVs.

Prints JSON with `tabs` (sheetId, title, source file, values: a 2D array for one write from A1;
formulas start with `=`, text the sheet would parse is prefixed with `'`), `requests` (one
batchUpdate: rename the first tab to Info, add the others, format) and `expected` (A1 cell ->
value its formula must read back as).

For an existing spreadsheet, delete every tab but the first and clear it, then pass that tab's id
as --info-sheet-id and a --first-sheet-id above every id the spreadsheet has used.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import (  # noqa: E402
    ALWAYS, LAUNCH_PROFILES, METHOD_ITEMS, METHOD_LABELS, ROW_PREFIX, SHEET_TABS, SOURCE_METHODS, STATUS_VALUES, TARGET_LABELS,
    TEST_TARGETS, column_width, confidence, display_header, methods_used, read_csv, read_header, run_targets,
    split_evidence,
)

HEADER_GREY = {"red": 0.9, "green": 0.9, "blue": 0.9}
INFO_WIDTHS = (260, 900, 140, 140)
NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
SOURCE_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+){2,}\.(?:py|cpp|hpp|cc|h|yaml)\b")
# Free-text columns whose source paths and evidence prefixes the view may shorten. Test node ids
# and log paths stay whole so they can be copied into a command.
PROSE_FIELDS = {"call_site", "grid_dependency", "dispatch_evidence", "evidence", "notes", "required_change",
                "blocker", "detail"}
# Columns a reader scans or that the Counts formulas read, kept even when every row has one value.
KEPT_FIELDS = {"id", "status", "launches"}
MERGED = "device_op / program_factory"
MERGED_HEADER = "Device op / program factory"
TRAILING_OP_FIELDS = ("grid_dependency", "op_code", "confidence", "dispatch_evidence", "origin")


def cell(text: str):
    if NUMBER.match(text):
        return float(text) if "." in text else int(text)
    if text and (text[0] in "+-=@'" or text[0].isdigit() or text.upper() in ("TRUE", "FALSE")):
        return "'" + text
    return text


def col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def tidy(text: str) -> str:
    measured, written = split_evidence(text)
    text = ROW_PREFIX.sub("", written)
    if measured:
        text = f"{measured}\n{text}"
    return SOURCE_PATH.sub(lambda m: "/".join(m.group(0).split("/")[-2:]), text)


def field(column: str) -> str:
    return column.split(":", 1)[-1]


def view_text(column: str, value: str) -> str:
    return tidy(value) if field(column) in PROSE_FIELDS else value


def short_op(device_op: str) -> str:
    return device_op.removeprefix("ttnn::")


def label(target: str) -> str:
    return TARGET_LABELS.get(target, target)


def describe(value) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k}: {describe(v)}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(describe(v) for v in value)
    return str(value)


CONFIDENCE_TEXT = {
    "high": "a graph capture of the entry test names the device op and program factory of every launch",
    "medium": ("a run of the entry test confirmed op codes (Tracy) or compiled kernels (kernel cache); "
               "program factories come from the static trace"),
    "low": "read from the model code at the pinned commit; nothing was run",
}


def method_state(run: dict, method: str) -> str:
    if method in METHOD_ITEMS and not run.get(METHOD_ITEMS[method][1]):
        mark = run.get("measurements", {}).get(METHOD_ITEMS[method][0], {})
        if mark.get("state") == "failed":
            return f"failed {mark.get('date', '')}: {mark.get('note', '')}".rstrip(": ")
    if method == "static":
        return f"done at {run.get('tt_metal_commit', '')[:7]}" if run.get("kind") == "static" else "not run"
    if method == "tracy":
        if not run.get("measured_ops"):
            return "not run"
        return f"done: {run.get('measured_inferences', 1)} inference(s) in the measured window"
    if method == "graph":
        g = run.get("graph_capture")
        if not g:
            return "not run"
        return (f"done {g['date']}: {g['launches']} launches over {g['inferences']} inference(s), "
                f"{g['kernel_ops']} (op code, program factory) pairs, {g['host_transfers_inside']} host transfers "
                "between the first and the last launch")
    c = run.get("kernel_cache")
    if not c:
        return "not run"
    return (f"done {c['date']}: {c['kernel_builds']} kernel builds ({c['compute_builds']} compute, "
            f"{c['dataflow_builds']} data movement), {c['compute_rows']} compute and {c['dataflow_rows']} dataflow "
            f"API rows, layers {', '.join(c['layers'])}")


def new_tab(title: str, source: str, values: list[list], widths: list[int], bold: list[int] | None = None,
            header: bool = True) -> dict:
    return {"title": title, "source": source, "values": values, "widths": widths,
            "bold": bold if bold is not None else [0], "header": header}


def op_table_view(rows: list[dict], header: list[str], targets: tuple[str, ...]):
    order = ["id", "stage", "ttnn_api", MERGED]
    for t in targets:
        order += [c for c in (f"{t}:shapes", f"{t}:launches") if c in header]
    order += ["call_site", "launch_condition"]
    for t in targets:
        order += [c for c in (f"{t}:status", f"{t}:tests", f"{t}:test_result", f"{t}:evidence") if c in header]
    order += list(TRAILING_OP_FIELDS)
    order += [c for c in header if c not in order and c not in ("device_op", "program_factory")]

    def raw(row: dict, column: str) -> str:
        if column == MERGED:
            return f"{short_op(row['device_op'])} / {row['program_factory']}"
        return row[column]

    # Constancy is judged on the CSV text, before shortening, so the view never claims two
    # different values are the same.
    constant: list[tuple[str, str]] = []
    columns = []
    for column in order:
        values = {raw(r, column) for r in rows}
        if len(rows) > 1 and len(values) == 1 and field(column) not in KEPT_FIELDS and column != MERGED:
            text = values.pop()
            if text:
                constant.append((display_header(column), view_text(column, text)))
            continue
        columns.append(column)
    headers = [MERGED_HEADER if c == MERGED else display_header(c) for c in columns]
    values = [headers] + [[cell(view_text(c, raw(r, c))) for c in columns] for r in rows]
    widths = [300 if c == MERGED else column_width(c) for c in columns]
    return new_tab("Op table", "op_table.csv", values, widths), columns, constant


def grouped_evidence(members: list[dict], column: str) -> str:
    texts: dict[str, list[str]] = collections.OrderedDict()
    for m in members:
        texts.setdefault(tidy(m[column]), []).append(m["id"])
    if len(texts) == 1:
        return next(iter(texts))
    return "\n".join(f"{'row' if len(ids) == 1 else 'rows'} {', '.join(ids)}: {text}" for text, ids in texts.items())


def kernel_ops_view(kernel_ops: list[dict], ops: list[dict], targets: tuple[str, ...], op_columns: list[str]):
    by_id = {r["id"]: r for r in ops}
    measured = any(k.get("measured_launches_op_code", "0") not in ("", "0") for k in kernel_ops)
    graphed = any(k.get("graph_launches", "0") not in ("", "0") for k in kernel_ops)
    # Evidence that is the same in every op-table row is on Info, not repeated per kernel op.
    shown = {t: f"{t}:evidence" in op_columns for t in targets}
    headers = ["#", MERGED_HEADER, "Stages", "ttnn API", f"Launches ({label(targets[0])})"]
    widths = [40, 300, 160, 180, 90]
    if measured:
        headers.append("Measured launches (op code)")
        widths.append(110)
    if graphed:
        headers.append("Graph launches per inference")
        widths.append(110)
    headers += ["Seen by", "Op table rows"]
    widths += [140, 120]
    for t in targets:
        headers.append(f"{label(t)}: status (rows)")
        widths.append(110)
        if shown[t]:
            headers.append(f"{label(t)}: evidence and gap")
            widths.append(420)
        if t in TEST_TARGETS:
            headers += [f"{label(t)}: kernel-op tests", f"{label(t)}: TTsim results"]
            widths += [300, 300]
    values = [headers]
    for k in kernel_ops:
        members = [by_id[i] for i in k["op_table_ids"].split(",") if i in by_id]
        stages = dict.fromkeys(m["stage"].split()[0] for m in members if m["stage"])
        apis = dict.fromkeys(m["ttnn_api"] for m in members if m["ttnn_api"])
        if k["program_factory"]:
            name = f"{short_op(k['device_op']) or k['op_code']} / {k['program_factory']}"
        else:
            name = k["op_code"]
        row = [cell(k["id"]), name, ", ".join(stages), ", ".join(apis), cell(k["static_launches"])]
        if measured:
            row.append(cell(k["measured_launches_op_code"]))
        if graphed:
            row.append(cell(k.get("graph_launches", "")))
        row += [k.get("sources", ""), cell(k["op_table_ids"].replace(",", ", "))]
        for t in targets:
            row.append(cell(k.get(f"{t}:statuses", "")))
            if shown[t]:
                row.append(cell(grouped_evidence(members, f"{t}:evidence")))
            if t in TEST_TARGETS:
                row += [cell(k.get(f"{t}:tests", "").replace("; ", "\n")),
                        cell(k.get(f"{t}:test_results", "").replace("; ", "\n"))]
        values.append(row)
    values.append(["Total", "", "", "", f"=SUM(E2:E{len(kernel_ops) + 1})"])
    return new_tab("Kernel ops", "kernel_ops.csv", values, widths, [0, len(values) - 1])


def csv_view(path: pathlib.Path, title: str) -> dict:
    columns = read_header(path)
    values = [[display_header(c) for c in columns]]
    values += [[cell(view_text(c, r[c])) for c in columns] for r in read_csv(path)]
    return new_tab(title, path.name, values, [column_width(c) for c in columns])


def summary_rows(path: pathlib.Path) -> tuple[str, list[list], list[int]]:
    """summary.md as label | text rows; heading rows are returned for bolding.

    The first table is the kernel-op table, shown as its own tab; later tables keep their cells.
    """
    title, rows, headings, paragraph = "", [], [], []
    in_code = in_table = seen_table = False
    last_item = False

    def clean(text: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("`", "")

    def flush():
        if paragraph:
            rows.append(["", cell(" ".join(paragraph))])
            paragraph.clear()

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            flush()
            in_code, last_item = not in_code, False
            continue
        if in_code:
            if s:
                rows.append(["", cell(s)])
            continue
        if s.startswith("|"):
            flush()
            if not in_table:
                in_table, skip = True, not seen_table
                seen_table = True
            if not skip and not re.fullmatch(r"\|[\s:|-]+\|", s):
                rows.append([cell(clean(c.strip())) for c in s.strip("|").split("|")])
            last_item = False
            continue
        in_table = False
        if not s:
            flush()
            last_item = False
            continue
        if last_item and line[:1].isspace():
            rows[-1][-1] = cell(f"{str(rows[-1][-1]).lstrip(chr(39))} {clean(s)}")
            continue
        heading = re.match(r"^(#+)\s+(.*)", s)
        if heading:
            flush()
            last_item = False
            if heading.group(1) == "#" and not title:
                title = clean(heading.group(2))
                continue
            rows.append([])
            headings.append(len(rows))
            rows.append([clean(heading.group(2))])
            continue
        item = re.match(r"^(?:[-*]|(\d+)\.)\s+(.*)", s)
        if item:
            flush()
            last_item = True
            text = clean(item.group(2))
            labelled = re.match(r"^([^:]{1,40}):\s+(.+)$", text)
            if item.group(1):
                rows.append([f"'{item.group(1)}.", cell(text)])
            elif labelled:
                rows.append([labelled.group(1), cell(labelled.group(2))])
            else:
                rows.append(["", cell(text)])
            continue
        last_item = False
        paragraph.append(clean(s))
    flush()
    return title, rows, headings


def info_view(run_dir: pathlib.Path, run: dict, targets: tuple[str, ...], op_columns: list[str],
              constant: list[tuple[str, str]], ops: list[dict], trace_path: pathlib.Path):
    source, destinations = targets[0], targets[1:]
    title, summary, summary_headings = summary_rows(run_dir / "summary.md")
    rows: list[list] = [[title or f"{run.get('model', 'Model')} kernel op analysis"]]
    bold = [0]
    profile = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in run.get("profile_values", {}).items()
                        if not isinstance(v, bool))
    commit = run.get("tt_metal_commit", "")
    detail = [x for x in (run.get("tt_metal_ref"), run.get("date")) if x]
    if detail:
        commit += f" ({', '.join(detail)})"
    meta = [("Model", run.get("model")), ("Implementation", run.get("implementation")),
            ("Entry test", run.get("test")), ("Source", f"{label(source)}: {profile}" if profile else label(source)),
            ("Destinations", ", ".join(label(d) for d in destinations)), ("tt-metal commit", commit),
            ("TTsim commit", run.get("ttsim_commit")),
            ("Entry test passes", describe(run["entry_test_passes"]) if run.get("entry_test_passes") else "")]
    rows += [[name, cell(str(v))] for name, v in meta if v]

    used = methods_used(run)
    level = confidence(used)
    rows.append([])
    bold.append(len(rows))
    rows.append(["Analysis methods", "State"])
    rows += [[METHOD_LABELS[m], cell(method_state(run, m))] for m in SOURCE_METHODS]
    rows.append(["Confidence", cell(f"{level}: {CONFIDENCE_TEXT[level]}")])
    bold.append(len(rows) - 1)

    # Counts: formulas over the Op table tab, so a reader can trace every number.
    expected: dict[str, int] = {}
    last = len(ops) + 1

    def rng(column: str) -> str:
        letter = col_letter(op_columns.index(column))
        return f"{quote('Op table')}!{letter}2:{letter}{last}"

    launch_targets = [t for t in targets if t == source or t in LAUNCH_PROFILES]
    rows.append([])
    bold.append(len(rows))
    rows.append(["Counts", "Op table rows"] + [f"Launches per inference ({label(t)})" for t in launch_targets])
    count_rows: list[tuple[str, str | None, str]] = [("All rows", None, "")]
    if "launch_condition" in op_columns:
        count_rows.append((f"Launch condition: {ALWAYS}", "launch_condition", ALWAYS))
    for t in targets:
        present = {r[f"{t}:status"] for r in ops}
        count_rows += [(f"{label(t)}: {s}", f"{t}:status", s) for s in STATUS_VALUES if s in present]
    for name, column, value in count_rows:
        members = ops if column is None else [r for r in ops if r[column] == value]
        rows.append([name, f"=COUNTA({rng('id')})" if column is None else f'=COUNTIF({rng(column)},"{value}")'])
        expected[f"Info!B{len(rows)}"] = len(members)
        for i, t in enumerate(launch_targets):
            launches = rng(f"{t}:launches")
            rows[-1].append(f"=SUM({launches})" if column is None else f'=SUMIFS({launches},{rng(column)},"{value}")')
            expected[f"Info!{col_letter(2 + i)}{len(rows)}"] = sum(int(m[f"{t}:launches"] or 0) for m in members)

    trace = read_csv(trace_path) if trace_path.exists() else []
    if trace:
        trace_columns = read_header(trace_path)
        end = len(trace) + 1

        def trace_rng(column: str) -> str:
            letter = col_letter(trace_columns.index(column))
            return f"{quote('Call trace')}!{letter}2:{letter}{end}"

        for i, t in enumerate(launch_targets):
            rows.append([f"Call trace launches ({label(t)})", ""] + [""] * len(launch_targets))
            rows[-1][2 + i] = (f'=SUMPRODUCT(({trace_rng("profile")}="{t}")*{trace_rng("repeats")}'
                               f'*{trace_rng("launches_per_repeat")})')
            expected[f"Info!{col_letter(2 + i)}{len(rows)}"] = sum(
                int(x["repeats"]) * int(x["launches_per_repeat"]) for x in trace if x["profile"] == t)

    offset = len(rows)
    rows += summary
    bold += [offset + h for h in summary_headings]
    if constant:
        rows.append([])
        bold.append(len(rows))
        rows.append(["Same in every op table row"])
        rows += [[name, cell(text)] for name, text in constant]
    return new_tab("Info", "run.json, summary.md", rows, list(INFO_WIDTHS), bold, header=False), expected


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


def _bold(sheet_id: int, row: int, columns: int, grey: bool) -> dict:
    fmt: dict = {"textFormat": {"bold": True}}
    fields = "userEnteredFormat.textFormat.bold"
    if grey:
        fmt["backgroundColorStyle"] = {"rgbColor": HEADER_GREY}
        fields += ",userEnteredFormat.backgroundColorStyle"
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1,
                                     "startColumnIndex": 0, "endColumnIndex": columns},
                           "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def spec(run_dir: pathlib.Path, info_id: int = 0, first_id: int = 1) -> dict:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    source, destinations = run_targets(run)
    targets = (source, *destinations)
    ops = read_csv(run_dir / "op_table.csv")
    op_tab, op_columns, constant = op_table_view(ops, read_header(run_dir / "op_table.csv"), targets)
    info_tab, expected = info_view(run_dir, run, targets, op_columns, constant, ops, run_dir / "call_trace.csv")

    tabs = [info_tab]
    kernel_ops_path = run_dir / "kernel_ops.csv"
    if kernel_ops_path.exists():
        kernel_ops = read_csv(kernel_ops_path)
        tabs.append(kernel_ops_view(kernel_ops, ops, targets, op_columns))
        expected[f"{quote('Kernel ops')}!E{len(kernel_ops) + 2}"] = sum(int(k["static_launches"] or 0)
                                                                       for k in kernel_ops)
    tabs.append(op_tab)
    for name, title in SHEET_TABS:
        if name in ("op_table", "kernel_ops"):
            continue
        if name == "kernel_test_results":
            for path in sorted(run_dir.glob("kernel_test_results_*.csv")):
                tabs.append(csv_view(path, f"Kernel tests ({label(path.stem.removeprefix('kernel_test_results_'))})"))
        elif (run_dir / f"{name}.csv").exists():
            tabs.append(csv_view(run_dir / f"{name}.csv", title))
    tabs += [csv_view(run_dir / f"changes_{name}.csv", f"Changes: {title}") for name, title in SHEET_TABS
             if (run_dir / f"changes_{name}.csv").exists()]

    # The kept first tab may be smaller than Info needs; size it like the added tabs.
    info_size = {"rowCount": len(info_tab["values"]) + 50, "columnCount": max(len(r) for r in info_tab["values"])}
    requests = [{"updateSheetProperties": {
        "properties": {"sheetId": info_id, "title": "Info", "gridProperties": info_size},
        "fields": "title,gridProperties.rowCount,gridProperties.columnCount"}}]
    for index, tab in enumerate(tabs):
        width = max(len(r) for r in tab["values"])
        tab["sheetId"] = info_id if index == 0 else first_id + index - 1
        if index:
            requests.append({"addSheet": {"properties": {
                "sheetId": tab["sheetId"], "title": tab["title"], "index": index,
                "gridProperties": {"rowCount": len(tab["values"]) + 50, "columnCount": width,
                                   "frozenRowCount": 1 if tab["header"] else 0}}}})
        requests.append(_wrap_all(tab["sheetId"]))
        requests += [_width(tab["sheetId"], i, w) for i, w in enumerate(tab.pop("widths")[:width])]
        header = tab.pop("header")
        requests += [_bold(tab["sheetId"], r, width, header and r == 0) for r in tab.pop("bold")]
    return {"tabs": tabs, "requests": requests, "expected": expected}


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
