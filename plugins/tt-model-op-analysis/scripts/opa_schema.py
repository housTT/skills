"""Column names, targets and status values shared by every tt-model-op-analysis script."""

from __future__ import annotations

import csv
import pathlib

# A run analyses one source implementation on one profile and, optionally, projects its kernel ops
# onto destinations. A destination is a profile running the same code, or `quasar-bf16`: the code
# with block-float dtypes turned into bfloat16 and mainline calls routed to Quasar forks.
PROFILES = ("p100", "p150", "n150", "n300-1chip", "quasar")
TARGET_LABELS = {
    "p100": "P100", "p150": "P150", "n150": "N150", "n300-1chip": "N300, one chip", "quasar": "Quasar",
    "quasar-bf16": "Quasar, BF16 projection",
}
TARGETS = tuple(TARGET_LABELS)
SOURCE_TARGETS = PROFILES
# Destinations whose launches are traced: profiles that run the same code with their own grid.
LAUNCH_PROFILES = ("p100", "p150", "n150", "n300-1chip")
TARGET_PROFILE = {t: t for t in PROFILES} | {"quasar-bf16": "quasar"}

STATUS_VALUES = ("✅", "⚠️", "❌", "n/a")
CONFIDENCE_VALUES = ("verified", "unverified")
ALWAYS = "always"
ORIGIN_VALUES = ("static", "measured", "static+measured")
NOT_TRACED = "not traced"
# Targets whose status is decided per kernel op from Quasar tests (references/target-status.md).
TEST_TARGETS = ("quasar", "quasar-bf16")
TEST_RESULT_VALUES = ("pass", "fail", "hang", "error", "xfail", "skip", "not run")

OP_TABLE_BASE = (
    "id", "stage", "origin", "ttnn_api", "op_code", "device_op", "program_factory",
    "call_site", "launch_condition", "confidence", "dispatch_evidence", "grid_dependency",
)
CALL_TRACE_COLUMNS = ("id", "profile", "stage", "ops", "repeats", "launches_per_repeat", "notes")
PORT_BLOCKERS_COLUMNS = ("id", "blocker", "affected_ids", "evidence", "required_change")
HOST_OPS_COLUMNS = ("id", "host_work", "when", "counts_as_host_op", "reason")
HOST_OP_VALUES = ("yes", "no")
MATCH_KEY = ("stage", "ttnn_api", "device_op", "program_factory")


def run_targets(run: dict) -> tuple[str, list[str]]:
    return run["source"], list(run.get("destinations", []))


def has_launches(target: str, is_source: bool) -> bool:
    return is_source or target in LAUNCH_PROFILES


def target_columns(target: str, is_source: bool) -> list[str]:
    columns = [f"{target}:shapes", f"{target}:launches"] if has_launches(target, is_source) else []
    if target in TEST_TARGETS:
        columns += [f"{target}:tests", f"{target}:test_result"]
    return columns + [f"{target}:status", f"{target}:evidence"]


def op_table_columns(source: str, destinations: list[str]) -> list[str]:
    columns = list(OP_TABLE_BASE) + target_columns(source, True)
    for destination in destinations:
        columns += target_columns(destination, False)
    return columns


def kernel_ops_columns(source: str, destinations: list[str]) -> list[str]:
    columns = ["id", "device_op", "program_factory", "op_code", "origin", "op_table_ids",
               "static_launches", "static_launches_op_code", "measured_launches_op_code"]
    for target in [source, *destinations]:
        columns.append(f"{target}:statuses")
        if target in TEST_TARGETS:
            columns += [f"{target}:tests", f"{target}:test_results"]
    return columns


# Spreadsheet presentation: tab order, readable headers and column widths in pixels.
SHEET_TABS = (
    ("op_table", "Op table"), ("kernel_ops", "Kernel ops"), ("kernel_test_results", "Kernel tests"),
    ("call_trace", "Call trace"), ("port_blockers", "Port blockers"),
    ("host_ops", "Host ops"), ("measured_ops", "Measured ops"), ("host_fallback", "Host fallback"),
    ("footprint", "Footprint"), ("diff", "Static vs measured"),
)
FIELD_LABELS = {
    "id": "#", "stage": "Stage", "origin": "Origin (static / measured)", "ttnn_api": "ttnn API", "op_code": "Op code (Tracy)",
    "device_op": "Device op", "program_factory": "Program factory", "call_site": "Call site (file:line)",
    "launch_condition": "Launch condition", "confidence": "Dispatch confidence",
    "dispatch_evidence": "Dispatch evidence", "grid_dependency": "Grid dependency",
    "launches_per_repeat": "Launches per repeat", "affected_ids": "Affected op table rows",
    "required_change": "Required change", "op_table_ids": "Op table rows",
    "static_launches": "Static launches",
    "static_launches_op_code": "Static launches (all factories of this op code)",
    "measured_launches_op_code": "Measured launches (op code)", "host_work": "Host work", "counts_as_host_op": "Counts as host op",
}
TARGET_FIELD_LABELS = {
    "shapes": "shapes and dtypes", "launches": "launches per inference",
    "status": "status", "evidence": "evidence and gap", "tests": "kernel-op tests", "test_result": "TTsim result",
    "statuses": "statuses (rows)", "test_results": "TTsim results",
}
FIELD_WIDTHS = {
    "id": 40, "stage": 120, "origin": 110, "tests": 300, "test_result": 90, "ttnn_api": 180, "op_code": 200, "device_op": 200, "program_factory": 260,
    "call_site": 220, "launch_condition": 180, "confidence": 100, "dispatch_evidence": 320,
    "grid_dependency": 220, "shapes": 260, "launches": 90, "status": 80, "evidence": 360,
    "ops": 220, "notes": 300, "blocker": 260, "required_change": 300, "host_work": 260, "reason": 320,
    "profile": 70, "repeats": 70, "launches_per_repeat": 90,
}
DEFAULT_WIDTH = 160


def display_header(column: str) -> str:
    if ":" in column:
        target, field = column.split(":", 1)
        return f"{TARGET_LABELS.get(target, target)}: {TARGET_FIELD_LABELS.get(field, field.replace('_', ' '))}"
    return FIELD_LABELS.get(column, column.replace("_", " ").capitalize())


def column_width(column: str) -> int:
    return FIELD_WIDTHS.get(column.split(":", 1)[-1], DEFAULT_WIDTH)


# utf-8-sig: reading tolerates a BOM, writing adds one so Excel and Numbers decode the
# status emoji as UTF-8 instead of Mac Roman.
CSV_ENCODING = "utf-8-sig"


def read_header(path: pathlib.Path) -> list[str]:
    with pathlib.Path(path).open(newline="", encoding=CSV_ENCODING) as fh:
        return next(csv.reader(fh), [])


def read_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with pathlib.Path(path).open(newline="", encoding=CSV_ENCODING) as fh:
        return list(csv.DictReader(fh))


def write_csv(path: pathlib.Path, columns: list[str], rows: list[dict]) -> None:
    with pathlib.Path(path).open("w", newline="", encoding=CSV_ENCODING) as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
