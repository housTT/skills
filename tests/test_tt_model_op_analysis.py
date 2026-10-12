"""Packaging and script behaviour for the tt-model-op-analysis plugin."""

from __future__ import annotations

import csv
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "tt-model-op-analysis"
SCRIPTS = PLUGIN / "scripts"
SKILL_NAMES = ("model-op-analysis",)
PHASES = ("static-phase.md", "measured-source.md", "measured-destination.md")


def test_plugin_has_one_skill_and_phase_references():
    assert sorted(p.name for p in (PLUGIN / "skills").iterdir()) == list(SKILL_NAMES)
    for name in PHASES:
        assert (PLUGIN / "references" / name).is_file()


def test_manifest_versions_match():
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    assert claude["version"] == codex["version"] == "0.3.0"


def test_shared_setup_is_step_by_step():
    setup = (PLUGIN / "references" / "run-setup.md").read_text()
    assert "Ask one decision per turn and stop for its answer" in setup
    assert "Ask which model to analyse. Do no model-specific discovery before the answer" in setup


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args],
                          capture_output=True, text=True)


def write_rows(path: pathlib.Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def op_row(i: int, launches_p150: int, port: str = "⚠️", port_ev: str = "fork x, no variant test",
           **overrides: str) -> dict:
    row = {
        "id": str(i), "stage": f"encoder {i}", "origin": "static", "ttnn_api": f"ttnn.op{i}",
        "op_code": f"Op{i}DeviceOperation",
        "device_op": f"prim::op{i}", "program_factory": f"Op{i}Factory", "call_site": f"model.py:{10 + i}",
        "launch_condition": "always", "confidence": "verified", "dispatch_evidence": f"op{i}.cpp select",
        "grid_dependency": "automatic",
        "p150:shapes": "(1,32)", "p150:launches": str(launches_p150), "p150:status": "✅",
        "p150:evidence": "entry test passes in CI",
        "quasar-port:port_op": "ttnn.experimental.quasar.op", "quasar-port:tests": "",
        "quasar-port:test_result": "not run", "quasar-port:status": port, "quasar-port:evidence": port_ev,
    }
    row.update(overrides)
    return row


BLOCKER_FIELDS = ["id", "blocker", "affected_ids", "evidence", "required_change"]
HOST_FIELDS = ["id", "host_work", "when", "counts_as_host_op", "reason"]


def make_static_run(tmp_path: pathlib.Path, op_rows: list[dict], trace_rows: list[dict],
                    destination: str = "quasar-port", blockers: list[dict] | None = None,
                    host_ops: list[dict] | None = None) -> pathlib.Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"kind": "static", "source": "p150", "destination": destination}))
    write_rows(run / "op_table.csv", op_rows)
    write_rows(run / "call_trace.csv", trace_rows)
    write_rows(run / "port_blockers.csv", blockers or [], BLOCKER_FIELDS)
    write_rows(run / "host_ops.csv", host_ops or [], HOST_FIELDS)
    return run


def trace_row(i: int, repeats: int, per_repeat: int, profile: str = "p150") -> dict:
    return {"id": str(i), "profile": profile, "stage": "encoder", "ops": "op1, op2",
            "repeats": str(repeats), "launches_per_repeat": str(per_repeat), "notes": ""}


def validate(run: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return run_script("validate_static.py", str(run))


def test_valid_static_run_passes_and_prints_range(tmp_path):
    rows = [op_row(1, 12), op_row(2, 1, launch_condition="skipped when the buffer does not move")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 12, 1), trace_row(2, 1, 1)])
    result = validate(run)
    assert result.returncode == 0, result.stdout
    assert result.stdout.splitlines() == ["OK", "p150: 13 launches per inference, 1 of them conditional (12-13); 0 measured-only launches"]


def test_legacy_targets_list_is_rejected(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    (run / "run.json").write_text(json.dumps({"targets": ["p150", "quasar"]}))
    assert "predates source/destination targets" in validate(run).stdout


def test_source_must_be_blackhole_and_differ(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    (run / "run.json").write_text(json.dumps({"source": "quasar-bf16", "destination": "quasar-bf16"}))
    out = validate(run).stdout
    assert "source 'quasar-bf16' not in" in out
    assert "source and destination must differ" in out


def test_origin_values_checked(tmp_path):
    out = validate(make_static_run(tmp_path, [op_row(1, 1, origin="guess")], [trace_row(1, 1, 1)])).stdout
    assert "row 1: origin 'guess' not in" in out


def test_measured_rows_excluded_from_trace_total(tmp_path):
    rows = [op_row(1, 2), op_row(2, 3, origin="measured", call_site="not traced", grid_dependency="not traced")]
    result = validate(make_static_run(tmp_path, rows, [trace_row(1, 1, 2)]))
    assert result.returncode == 0, result.stdout
    assert "p150: 2 launches per inference" in result.stdout
    assert "3 measured-only launches" in result.stdout


def test_test_result_must_match_status(tmp_path):
    rows = [op_row(1, 1, **{"quasar-port:tests": "t.py::a", "quasar-port:test_result": "fail"}),
            op_row(2, 1, port="✅", **{"quasar-port:tests": "t.py::b", "quasar-port:test_result": "pass"})]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 2, 1)])).stdout
    assert "row 1: quasar-port:test_result 'fail' requires status ❌" in out
    assert "row 2:" not in out


def test_tests_column_needs_node_ids(tmp_path):
    rows = [op_row(1, 1, **{"quasar-port:tests": "some test"})]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])).stdout
    assert "row 1: quasar-port:tests 'some test' is not a list of pytest node ids" in out


def test_pre_origin_run_is_rejected_with_upgrade_hint(tmp_path):
    rows = [op_row(1, 1)]
    del rows[0]["origin"]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])).stdout
    assert "op_table.csv missing column origin" in out
    assert "upgrade_run.py" in out


def test_off_by_one_id_fails(tmp_path):
    rows = [op_row(1, 12), op_row(2, 1)]
    rows[1]["id"] = "3"
    run = make_static_run(tmp_path, rows, [trace_row(1, 12, 1), trace_row(2, 1, 1)])
    result = validate(run)
    assert result.returncode == 1
    assert "op_table.csv row 2 has id 3" in result.stdout


def test_launch_total_mismatch_fails(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 12), op_row(2, 1)], [trace_row(1, 12, 1), trace_row(2, 2, 1)])
    result = validate(run)
    assert result.returncode == 1
    assert "p150: op_table launches 13 != call_trace launches 14" in result.stdout


def test_missing_and_unexpected_columns_fail(tmp_path):
    rows = [op_row(1, 1)]
    rows[0]["evidence"] = rows[0].pop("dispatch_evidence")
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])
    out = validate(run).stdout
    assert "op_table.csv missing column dispatch_evidence" in out
    assert "op_table.csv unexpected column evidence" in out


def test_side_tables_follow_schema(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    write_rows(run / "host_ops.csv", [{"id": "1", "stage": "s", "operation": "o"}])
    assert "host_ops.csv missing column host_work" in validate(run).stdout


def test_short_row_is_reported_not_crashing(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, port_ev="fork")], [trace_row(1, 1, 1)])
    text = (run / "op_table.csv").read_text(encoding="utf-8").splitlines()
    (run / "op_table.csv").write_text("\n".join([text[0], text[1].rsplit(",", 1)[0]]) + "\n", encoding="utf-8")
    result = validate(run)
    assert result.returncode == 1
    assert "op_table.csv row 1: field count differs from the header" in result.stdout
    assert "Traceback" not in result.stderr


def test_every_status_needs_its_own_evidence(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, port_ev="")], [trace_row(1, 1, 1)])
    assert "op_table.csv row 1: quasar-port:status without quasar-port:evidence" in validate(run).stdout


def test_port_only_status_value_is_gone(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, port="variant not used by port")], [trace_row(1, 1, 1)])
    assert "quasar-port:status 'variant not used by port' not in" in validate(run).stdout


def test_port_op_required_unless_na(tmp_path):
    rows = [op_row(1, 1, **{"quasar-port:port_op": ""}),
            op_row(2, 1, port="n/a", port_ev="Blackhole-only branch", **{"quasar-port:port_op": ""})]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 2, 1)])).stdout
    assert "row 1: quasar-port:port_op empty" in out
    assert "row 2: quasar-port:port_op" not in out


def test_unknown_status_fails(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, **{"p150:status": "ok"})], [trace_row(1, 1, 1)])
    assert "op_table.csv row 1: p150:status 'ok' not in" in validate(run).stdout


def test_na_exactly_when_no_launches(tmp_path):
    rows = [op_row(1, 1, **{"p150:status": "n/a"}), op_row(2, 0)]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])).stdout
    assert "row 1: p150 has 1 launches with status 'n/a'" in out
    assert "row 2: p150 has 0 launches with status '✅'" in out


def test_bad_launch_count_reported_once(tmp_path):
    out = validate(make_static_run(tmp_path, [op_row(1, 1, **{"p150:launches": "2x"})],
                                   [trace_row(1, 1, 1)])).stdout
    assert out.count("'2x' is not an integer") == 1
    assert "launches with status" not in out


def test_call_site_needs_line(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, call_site="model.py: use_transpose_as_fold=True")],
                          [trace_row(1, 1, 1)])
    assert "call_site 'model.py: use_transpose_as_fold=True' has no <file>:<line>" in validate(run).stdout


def test_grid_dependency_values(tmp_path):
    rows = [op_row(1, 1, grid_dependency="profile-derived"), op_row(2, 1, grid_dependency="see bf16 note"),
            op_row(3, 1, grid_dependency="none"), op_row(4, 1, grid_dependency="8x4 CoreGrid, model.py:29")]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 4, 1)])).stdout
    assert "row 1: grid_dependency 'profile-derived'" in out
    assert "row 2: grid_dependency 'see bf16 note'" in out
    assert "row 3: grid_dependency" not in out and "row 4: grid_dependency" not in out


def test_launch_condition_must_be_exact(tmp_path):
    rows = [op_row(1, 1, launch_condition="Always"), op_row(2, 1, launch_condition="")]
    out = validate(make_static_run(tmp_path, rows, [trace_row(1, 2, 1)])).stdout
    assert "row 1: launch_condition 'Always' must be exactly 'always'" in out
    assert "row 2: launch_condition empty" in out


def test_blocker_ids_must_exist(tmp_path):
    blockers = [{"id": "1", "blocker": "b", "affected_ids": "1, 9,", "evidence": "e", "required_change": "c"},
                {"id": "2", "blocker": "b", "affected_ids": "all", "evidence": "e", "required_change": "c"},
                {"id": "3", "blocker": "b", "affected_ids": "", "evidence": "e", "required_change": "c"}]
    out = validate(make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)], blockers=blockers)).stdout
    assert "port_blockers.csv row 1: affected id '9' is not an op_table id" in out
    assert "affected id ''" not in out
    assert "row 2:" not in out
    assert "port_blockers.csv row 3: affected_ids empty" in out


def test_host_op_flag_values(tmp_path):
    host = [{"id": "1", "host_work": "kwargs", "when": "per block", "counts_as_host_op": "maybe", "reason": "r"}]
    out = validate(make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)], host_ops=host)).stdout
    assert "counts_as_host_op 'maybe' not in" in out


def test_repeated_destination_evidence_warns(tmp_path):
    rows = [op_row(i, 1, port_ev="same text") for i in range(1, 6)]
    result = validate(make_static_run(tmp_path, rows, [trace_row(1, 5, 1)]))
    assert result.returncode == 0, result.stdout
    assert "WARNING: op_table.csv quasar-port:evidence: the same text in 5 of 5 rows" in result.stdout


def test_blackhole_destination_needs_its_trace(tmp_path):
    rows = [op_row(1, 2, **{"p100:shapes": "(1,32)", "p100:launches": "1", "p100:status": "✅",
                            "p100:evidence": "CI"})]
    for col in ("quasar-port:port_op", "quasar-port:tests", "quasar-port:test_result",
                "quasar-port:status", "quasar-port:evidence"):
        del rows[0][col]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 2)], destination="p100")
    assert "p100: op_table launches 1 != call_trace launches 0" in validate(run).stdout
    write_rows(run / "call_trace.csv", [trace_row(1, 1, 2), trace_row(2, 1, 1, profile="p100")])
    assert validate(run).returncode == 0


def test_sheet_spec_layout(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    write_rows(run / "changes_op_table.csv", [{"change": "added", "key": "k", "column": "", "old": "", "new": ""}])
    result = run_script("sheet_spec.py", str(run), "--summary-sheet-id", "7", "--first-sheet-id", "20")
    assert result.returncode == 0, result.stderr
    spec = json.loads(result.stdout)
    assert [(t["tab"], t["sheetId"]) for t in spec["tabs"]] == [
        ("Op table", 20), ("Call trace", 21), ("Port blockers", 22), ("Host ops", 23), ("Changes: Op table", 24)]
    headers = spec["tabs"][0]["headers"]
    assert "Call site (file:line)" in headers
    assert "Quasar, existing port: evidence and gap" in headers
    assert spec["tabs"][2]["headers"][3] == "Evidence"
    reqs = spec["requests"]
    assert reqs[0]["updateSheetProperties"]["properties"]["sheetId"] == 7
    assert not any("autoResizeDimensions" in r for r in reqs)
    wrapped = {r["repeatCell"]["range"]["sheetId"] for r in reqs
               if "repeatCell" in r and r["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP"}
    assert wrapped == {7, 20, 21, 22, 23, 24}


TENSOR_FIELDS = ["W_PAD[LOGICAL]", "Z_PAD[LOGICAL]", "Y_PAD[LOGICAL]", "X_PAD[LOGICAL]",
                 "LAYOUT", "DATATYPE", "MEMORY"]
OPS_HEADER = (["OP CODE", "OP TYPE", "ATTRIBUTES", "CORE COUNT", "HOST START TS", "HOST END TS",
               "HOST DURATION [ns]", "DEVICE KERNEL DURATION [ns]"]
              + [f"INPUT_{i}_{f}" for i in range(2) for f in TENSOR_FIELDS]
              + [f"OUTPUT_0_{f}" for f in TENSOR_FIELDS])
TENSOR_A = ["1[1]", "10[10]", "32[4]", "64[64]", "TILE", "BFLOAT16", "DEV_1_L1_HEIGHT_SHARDED"]
NO_TENSOR = [""] * len(TENSOR_FIELDS)


def ops_csv(path: pathlib.Path, rows: list[list]) -> pathlib.Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(OPS_HEADER)
        writer.writerows(rows)
    return path


def dev(code, start, end, cores, kernel_ns, host_ns=100):
    return [code, "tt_dnn_device", "{'program_config': 'MatmulMultiCoreReuseMultiCastProgramConfig'}",
            cores, start, end, host_ns, kernel_ns] + TENSOR_A + NO_TENSOR + TENSOR_A


def host(code, start, end, host_ns):
    return [code, "python_fallback", "", "", start, end, host_ns, ""] + NO_TENSOR * 3


def sign(name, ts):
    return [name, "signpost", "", "", ts, "", "", ""] + NO_TENSOR * 3


def read(path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_warm_window_uses_second_iteration_only(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [
        dev("MatmulDeviceOperation", 0, 10, 120, 5_000_000),
        sign("start", 20),
        dev("MatmulDeviceOperation", 1_000_000, 2_000_000, 120, 1_000_000),
        host("TorchPermute", 2_000_000, 3_000_000, 1_000_000),
        dev("SoftmaxDeviceOperation", 3_000_000, 5_000_000, 64, 500_000),
        sign("end", 6_000_000),
    ])
    out = tmp_path / "out"
    result = run_script("tracy_report.py", str(src), str(out), "--start", "start", "--end", "end")
    assert result.returncode == 0, result.stdout + result.stderr
    measured = read(out / "measured_ops.csv")
    assert [r["op_code"] for r in measured] == ["MatmulDeviceOperation", "TorchPermute", "SoftmaxDeviceOperation"]
    fb = read(out / "host_fallback.csv")[0]
    assert fb["window"] == "warm (signposts start..end)"
    assert (fb["device_ops"], fb["host_ops"]) == ("2", "1")
    assert fb["device_op_time_ms"] == "1.500"
    assert fb["host_op_time_ms"] == "1.000"
    assert fb["total_time_ms"] == "4.000"
    assert fb["host_pct_of_total"] == "25.00"
    assert "threshold" not in (out / "host_fallback.csv").read_text()


def test_inputs_outputs_from_expanded_columns(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    row = read(out / "measured_ops.csv")[0]
    tensor = "1[1]x10[10]x32[4]x64[64] TILE BFLOAT16 DEV_1_L1_HEIGHT_SHARDED"
    assert row["inputs"] == tensor
    assert row["outputs"] == tensor


def test_no_signposts_is_cold_whole_csv(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1_000_000, 8, 1_000_000)])
    out = tmp_path / "out"
    result = run_script("tracy_report.py", str(src), str(out))
    assert result.returncode == 0, result.stderr
    assert read(out / "host_fallback.csv")[0]["window"] == "cold (no signposts)"
    assert len(read(out / "measured_ops.csv")) == 1


def test_missing_named_signpost_fails(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    result = run_script("tracy_report.py", str(src), str(tmp_path / "out"), "--start", "start", "--end", "end")
    assert result.returncode == 2
    assert "ERROR: signpost 'start' not found" in result.stdout


def test_footprint_max_cores_and_unmeasured_dram(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [
        dev("MatmulDeviceOperation", 0, 1, 64, 1), dev("MatmulDeviceOperation", 1, 2, 120, 1),
    ])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    fp = read(out / "footprint.csv")
    assert fp == [{"op_code": "MatmulDeviceOperation", "launches": "2", "max_core_count": "120",
                   "peak_dram_mb": "not measured"}]


def test_footprint_uses_given_peak_dram(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 64, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out), "--peak-dram-mb", "198").returncode == 0
    assert read(out / "footprint.csv")[0]["peak_dram_mb"] == "198.0"


MEASURED_FIELDS = ["id", "op_code", "op_type", "attributes", "inputs", "outputs",
                   "core_count", "device_kernel_ns", "host_ns"]


def static_table(path, rows):
    base = {"stage": "s", "ttnn_api": "a", "device_op": "d", "call_site": "", "dispatch_evidence": "e",
            "confidence": "verified", "grid_dependency": "", "p150:shapes": "", "p150:status": "✅"}
    write_rows(path, [{"id": str(i), **base, **r} for i, r in enumerate(rows, 1)])
    return path


def measured_table(path, rows):
    write_rows(path, [{"id": str(i), "op_code": c, "op_type": "tt_dnn_device", "attributes": a,
                       "inputs": "", "outputs": "", "core_count": "1", "device_kernel_ns": "1", "host_ns": "1"}
                      for i, (c, a) in enumerate(rows, 1)], fieldnames=MEASURED_FIELDS)
    return path


def run_diff(tmp_path, static_rows, measured_rows):
    s = static_table(tmp_path / "op_table.csv", static_rows)
    m = measured_table(tmp_path / "measured_ops.csv", measured_rows)
    out = tmp_path / "diff.csv"
    result = run_script("diff_reports.py", "--static", str(s), "--measured", str(m),
                        "--profile", "p150", "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    return read(out)


def test_diff_categories(tmp_path):
    rows = run_diff(tmp_path,
        [{"op_code": "MatmulDeviceOperation", "program_factory": "MatmulMultiCoreReuseMcast2DProgramFactory (cfg)", "p150:launches": "2"},
         {"op_code": "SoftmaxDeviceOperation", "program_factory": "SoftmaxShardedProgramFactory", "p150:launches": "1"},
         {"op_code": "ReshardDeviceOperation", "program_factory": "ReshardGenericFactory", "p150:launches": "1"}],
        [("MatmulDeviceOperation", "MatmulMultiCoreReuseMcast2DProgramFactory"),
         ("SoftmaxDeviceOperation", "other"), ("SoftmaxDeviceOperation", "other"),
         ("TilizeDeviceOperation", "")])
    got = {(r["category"], r["op_code"]) for r in rows}
    assert got == {
        ("count differs", "MatmulDeviceOperation"),
        ("count differs", "SoftmaxDeviceOperation"),
        ("missing in measured", "ReshardDeviceOperation"),
        ("extra in measured", "TilizeDeviceOperation"),
    }


def test_static_row_without_op_code_reported(tmp_path):
    rows = run_diff(tmp_path, [{"op_code": "", "program_factory": "X", "p150:launches": "1"}], [])
    assert [(r["category"], r["static_ids"]) for r in rows] == [("unmatched static row", "1")]


def test_identical_tables_have_no_diff(tmp_path):
    rows = run_diff(tmp_path,
        [{"op_code": "MatmulDeviceOperation", "program_factory": "Mcast2D", "p150:launches": "1"}],
        [("MatmulDeviceOperation", "uses Mcast2D")])
    assert rows == []


def cmp_rows(**overrides):
    row = {"id": "1", "stage": "enc", "ttnn_api": "ttnn.linear", "device_op": "prim::matmul",
           "program_factory": "Mcast2D", "p150:launches": "12", "p150:status": "✅"}
    row.update(overrides)
    return row


def run_compare(tmp_path, old, new):
    write_rows(tmp_path / "old.csv", old)
    write_rows(tmp_path / "new.csv", new)
    out = tmp_path / "changes.csv"
    result = run_script("compare_runs.py", "--old", str(tmp_path / "old.csv"),
                        "--new", str(tmp_path / "new.csv"), "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    return read(out)


def test_reordered_rows_no_change(tmp_path):
    a = cmp_rows(id="1")
    b = cmp_rows(id="2", ttnn_api="ttnn.add", device_op="prim::binary_ng", program_factory="BinaryNg")
    assert run_compare(tmp_path, [a, b], [dict(b, id="1"), dict(a, id="2")]) == []


def test_duplicate_keys_reordered_no_change(tmp_path):
    a = cmp_rows(id="1", **{"p150:shapes": "64"})
    b = cmp_rows(id="2", **{"p150:shapes": "128"})
    assert run_compare(tmp_path, [a, b], [dict(b, id="1"), dict(a, id="2")]) == []


def test_missing_key_column_fails(tmp_path):
    write_rows(tmp_path / "old.csv", [{"id": "1", "profile": "p150", "stage": "s"}])
    write_rows(tmp_path / "new.csv", [{"id": "1", "profile": "p150", "stage": "s"}])
    result = run_script("compare_runs.py", "--old", str(tmp_path / "old.csv"), "--new", str(tmp_path / "new.csv"),
                        "--out", str(tmp_path / "changes.csv"))
    assert result.returncode == 2
    assert "ERROR: key column ttnn_api missing" in result.stdout


def test_added_removed_changed(tmp_path):
    old = [cmp_rows(), cmp_rows(ttnn_api="ttnn.add", device_op="prim::binary_ng", program_factory="BinaryNg")]
    new = [cmp_rows(**{"p150:launches": "13"}),
           cmp_rows(ttnn_api="ttnn.softmax", device_op="prim::softmax", program_factory="Sharded")]
    rows = run_compare(tmp_path, old, new)
    got = {(r["change"], r["column"], r["old"], r["new"]) for r in rows}
    assert ("changed", "p150:launches", "12", "13") in got
    assert any(r["change"] == "added" and "ttnn.softmax" in r["key"] for r in rows)
    assert any(r["change"] == "removed" and "ttnn.add" in r["key"] for r in rows)


REFERENCES = PLUGIN / "references"
LINE_CITATION = re.compile(r"\.(?:cpp|hpp|h|py|rst|sh)\s*:\s*\d+")
PLUGIN_ROOT_REF = re.compile(r"<plugin-root>/((?:references|scripts)/[\w./-]+)")


def _unfenced_lines(path):
    fenced = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and not line.lstrip().startswith(">"):
            yield n, line


def test_references_cite_no_line_numbers():
    paths = sorted(REFERENCES.glob("*.md"))
    assert paths
    for path in paths:
        for n, line in _unfenced_lines(path):
            assert not LINE_CITATION.search(line), f"{path.name}:{n} cites a source line number"


def test_plugin_root_links_resolve():
    files = list(PLUGIN.glob("skills/*/SKILL.md")) + list(REFERENCES.glob("*.md"))
    seen = 0
    for path in files:
        for rel in PLUGIN_ROOT_REF.findall(path.read_text(encoding="utf-8")):
            seen += 1
            assert (PLUGIN / rel).is_file(), f"{path}: {rel} does not exist"
    assert seen


def test_schemas_doc_lists_every_column():
    sys.path.insert(0, str(SCRIPTS))
    import compare_runs
    import diff_reports
    import opa_schema
    import kernel_tests
    import tracy_report
    doc = (REFERENCES / "schemas.md").read_text(encoding="utf-8")
    names = (set(opa_schema.OP_TABLE_BASE) | set(opa_schema.CALL_TRACE_COLUMNS)
             | set(opa_schema.PORT_BLOCKERS_COLUMNS) | set(opa_schema.HOST_OPS_COLUMNS)
             | set(tracy_report.MEASURED_COLUMNS) | set(tracy_report.HOST_FALLBACK_COLUMNS)
             | set(tracy_report.FOOTPRINT_COLUMNS) | set(diff_reports.DIFF_COLUMNS)
             | set(compare_runs.CHANGE_COLUMNS) | set(kernel_tests.KERNEL_TEST_COLUMNS)
             | {"op_table_ids", "static_launches", "static_launches_op_code", "measured_launches_op_code"})
    missing = sorted(n for n in names if f"`{n}`" not in doc)
    assert not missing, f"schemas.md does not document {missing}"


def test_skill_offers_three_modes_and_names_its_gates():
    text = (PLUGIN / "skills" / "model-op-analysis" / "SKILL.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 120
    for mode in ("static", "static + measured", "measured over an existing static run"):
        assert mode in text
    for gate in ("validate_static.py", "same_commit.py", "merge_measured.py", "kernel_tests.py", "kernel_ops.py"):
        assert f"<plugin-root>/scripts/{gate}" in text
    assert "`<plugin-root>` is the `tt-model-op-analysis` plugin directory" in text


def test_measured_destination_runs_no_port_e2e():
    text = (PLUGIN / "references" / "measured-destination.md").read_text(encoding="utf-8")
    assert "kernel-op tests" in text
    assert "Do not run the port's whole-model test" in text


def test_root_readme_installs_plugin_on_both_hosts():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "codex plugin add tt-model-op-analysis@tenstorrent-skills" in readme
    assert "/plugin install tt-model-op-analysis@tenstorrent-skills" in readme


def test_validator_accepts_bom_csv(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    for name in ("op_table.csv", "call_trace.csv"):
        path = run / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    result = validate(run)
    assert result.returncode == 0, result.stdout


def test_written_csvs_carry_bom_for_spreadsheet_apps(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    for name in ("measured_ops.csv", "host_fallback.csv", "footprint.csv"):
        assert (out / name).read_bytes().startswith(b"\xef\xbb\xbf"), name


def test_kernel_ops_aggregates_by_device_op_and_factory(tmp_path):
    rows = [op_row(1, 2, op_code="HaloDeviceOperation", device_op="prim::halo", program_factory="Halo"),
            op_row(2, 3, op_code="HaloDeviceOperation", device_op="prim::halo", program_factory="Halo",
                   port="❌", **{"quasar-port:tests": "t.py::h", "quasar-port:test_result": "hang"}),
            op_row(3, 1, op_code="MoveDeviceOperation", device_op="prim::move", program_factory="Move")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 6)])
    m = measured_table(tmp_path / "measured_ops.csv",
                       [("HaloDeviceOperation", "")] * 4 + [("TilizeDeviceOperation", "")])
    result = run_script("kernel_ops.py", str(run), "--measured", str(m))
    assert result.returncode == 0, result.stderr
    got = read(run / "kernel_ops.csv")
    assert [(r["device_op"], r["op_table_ids"], r["static_launches"], r["measured_launches_op_code"])
            for r in got] == [("prim::halo", "1,2", "5", "4"), ("prim::move", "3", "1", "0"),
                              ("", "", "0", "1")]
    assert got[0]["quasar-port:statuses"] == "⚠️ 1; ❌ 1"
    assert got[0]["quasar-port:test_results"] == "t.py::h: hang"
    assert got[2]["origin"] == "measured" and got[2]["op_code"] == "TilizeDeviceOperation"


def measured_seq(path, codes):
    return measured_table(path, [(c, "") for c in codes])


def test_merge_marks_static_rows_seen(tmp_path):
    rows = [op_row(1, 1, op_code="A"), op_row(2, 1, op_code="B")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 2, 1)])
    m = measured_seq(tmp_path / "m.csv", ["A"])
    assert run_script("merge_measured.py", str(run), str(m)).returncode == 0
    got = read(run / "op_table.csv")
    assert [r["origin"] for r in got] == ["static+measured", "static"]


def test_merge_inserts_measured_only_in_order(tmp_path):
    rows = [op_row(1, 1, op_code="A"), op_row(2, 1, op_code="B")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 2, 1)])
    m = measured_seq(tmp_path / "m.csv", ["A", "X", "X", "B"])
    assert run_script("merge_measured.py", str(run), str(m)).returncode == 0
    got = read(run / "op_table.csv")
    assert [(r["id"], r["op_code"], r["origin"], r["p150:launches"]) for r in got] == [
        ("1", "A", "static+measured", "1"), ("2", "X", "measured", "2"), ("3", "B", "static+measured", "1")]
    assert got[1]["call_site"] == "not traced"
    assert got[1]["quasar-port:test_result"] == "not run"
    assert validate(run).returncode == 0, validate(run).stdout
    assert json.loads((run / "run.json").read_text())["measured_ops"] == str(m)


def test_merge_is_idempotent(tmp_path):
    rows = [op_row(1, 1, op_code="A")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])
    m = measured_seq(tmp_path / "m.csv", ["A", "X"])
    run_script("merge_measured.py", str(run), str(m))
    run_script("merge_measured.py", str(run), str(m))
    assert [r["op_code"] for r in read(run / "op_table.csv")] == ["A", "X"]


FAKE_PYTEST = """
import pathlib, sys, time
node = sys.argv[-1]
xml = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--junitxml="))
if "sleep" in node:
    time.sleep(30)
# xfail first: "xfail_c" also contains "fail".
child = {"xfail": "<skipped type='pytest.xfail' message='x'/>", "pass": "", "fail": "<failure message='x'/>"}
kind = next(k for k in child if k in node)
pathlib.Path(xml).write_text(f"<testsuites><testsuite><testcase name='t'>{child[kind]}</testcase></testsuite></testsuites>")
sys.exit(0 if kind != "fail" else 1)
"""


def kernel_test_run(tmp_path, tests_per_row):
    rows = [op_row(i, 1, **{"quasar-port:tests": t}) for i, t in enumerate(tests_per_row, 1)]
    run = make_static_run(tmp_path, rows, [trace_row(1, len(rows), 1)])
    data = json.loads((run / "run.json").read_text())
    data["ttsim_environment"] = {"TT_METAL_SIMULATOR": "/x/libttsim.so"}
    (run / "run.json").write_text(json.dumps(data))
    _, sha = git_repo(tmp_path)
    data["tt_metal_commit"] = sha
    (run / "run.json").write_text(json.dumps(data))
    fake = tmp_path / "fake_pytest.py"
    fake.write_text(FAKE_PYTEST)
    return run, f"{sys.executable} {fake}"


def test_kernel_tests_plan_dedups_nodes(tmp_path):
    run, _ = kernel_test_run(tmp_path, ["t.py::pass_a", "t.py::pass_a; t.py::fail_b"])
    assert run_script("kernel_tests.py", "plan", str(run)).returncode == 0
    plan = read(run / "kernel_tests_plan.csv")
    assert [(p["node_id"], p["op_table_ids"]) for p in plan] == [("t.py::pass_a", "1,2"), ("t.py::fail_b", "2")]


def test_kernel_tests_run_and_apply(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a", "t.py::fail_b", "t.py::xfail_c"])
    run_script("kernel_tests.py", "plan", str(run))
    r = run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(tmp_path / "tt-metal"), "--pytest-cmd", cmd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [x["result"] for x in read(run / "kernel_test_results.csv")] == ["pass", "fail", "xfail"]
    assert run_script("kernel_tests.py", "apply", str(run)).returncode == 0
    got = read(run / "op_table.csv")
    assert [(g["quasar-port:test_result"], g["quasar-port:status"]) for g in got] == [
        ("pass", "✅"), ("fail", "❌"), ("xfail", "❌")]
    assert got[1]["quasar-port:evidence"].startswith("TTsim run: t.py::fail_b fail")
    assert validate(run).returncode == 0, validate(run).stdout


def test_kernel_tests_timeout_is_hang(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::sleep_pass", "t.py::pass_b"])
    run_script("kernel_tests.py", "plan", str(run))
    run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(tmp_path / "tt-metal"), "--pytest-cmd", cmd,
               "--timeout", "2")
    assert [x["result"] for x in read(run / "kernel_test_results.csv")] == ["hang", "pass"]


def test_kernel_tests_run_resumes(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a"])
    run_script("kernel_tests.py", "plan", str(run))
    run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(tmp_path / "tt-metal"), "--pytest-cmd", cmd)
    out = run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(tmp_path / "tt-metal"), "--pytest-cmd", cmd).stdout
    assert "1 already done, 0 run" in out


def test_kernel_tests_run_needs_environment(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a"])
    data = json.loads((run / "run.json").read_text())
    del data["ttsim_environment"]
    (run / "run.json").write_text(json.dumps(data))
    run_script("kernel_tests.py", "plan", str(run))
    r = run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(tmp_path / "tt-metal"), "--pytest-cmd", cmd)
    assert r.returncode == 2 and "ttsim_environment" in r.stdout


def git_repo(tmp_path):
    repo = tmp_path / "tt-metal"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "a.txt").write_text("1")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha


def same_commit_run(tmp_path, sha):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"tt_metal_commit": sha}))
    return run


def test_same_commit_ok(tmp_path):
    repo, sha = git_repo(tmp_path)
    r = run_script("same_commit.py", str(same_commit_run(tmp_path, sha)), "--tt-metal", str(repo))
    assert r.returncode == 0, r.stdout


def test_same_commit_rejects_other_sha(tmp_path):
    repo, _ = git_repo(tmp_path)
    r = run_script("same_commit.py", str(same_commit_run(tmp_path, "0" * 40)), "--tt-metal", str(repo))
    assert r.returncode == 1 and "0000000" in r.stdout


def test_same_commit_rejects_local_changes(tmp_path):
    repo, sha = git_repo(tmp_path)
    (repo / "a.txt").write_text("2")
    r = run_script("same_commit.py", str(same_commit_run(tmp_path, sha)), "--tt-metal", str(repo))
    assert r.returncode == 1 and "modified" in r.stdout


def test_upgrade_run_adds_new_columns(tmp_path):
    rows = [op_row(1, 1)]
    for col in ("origin", "quasar-port:tests", "quasar-port:test_result"):
        del rows[0][col]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])
    assert validate(run).returncode == 1
    assert run_script("upgrade_run.py", str(run)).returncode == 0
    assert run_script("upgrade_run.py", str(run)).returncode == 0
    assert validate(run).returncode == 0
    got = read(run / "op_table.csv")[0]
    assert (got["origin"], got["quasar-port:test_result"]) == ("static", "not run")


def test_sheet_spec_puts_kernel_ops_in_summary(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    assert run_script("kernel_ops.py", str(run)).returncode == 0
    write_rows(run / "kernel_test_results.csv",
               [{"id": "1", "node_id": "t.py::a", "op_table_ids": "1", "result": "pass", "duration_s": "1", "log": "x"}])
    spec = json.loads(run_script("sheet_spec.py", str(run)).stdout)
    assert [t["tab"] for t in spec["tabs"]] == ["Op table", "Kernel tests", "Call trace", "Port blockers", "Host ops"]
    assert spec["summary_tables"][0]["csv"] == "kernel_ops.csv"
    assert "Quasar, existing port: statuses (rows)" in spec["summary_tables"][0]["headers"]


# Final-review fixes


def test_pre_target_run_is_refused_clearly(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    (run / "run.json").write_text(json.dumps({"targets": ["p150", "quasar"]}))
    v = validate(run)
    assert "predates source/destination targets" in v.stdout
    u = run_script("upgrade_run.py", str(run))
    assert u.returncode == 2 and "start a new static run" in u.stdout and "Traceback" not in u.stderr


def test_merge_inserts_after_the_occurrence_that_ran(tmp_path):
    rows = [op_row(1, 1, op_code="A", stage="s1 a"), op_row(2, 1, op_code="B", stage="s1 b"),
            op_row(3, 1, op_code="A", stage="s2 a"), op_row(4, 1, op_code="C", stage="s2 c")]
    run = make_static_run(tmp_path, rows, [trace_row(1, 4, 1)])
    m = measured_seq(tmp_path / "m.csv", ["A", "X", "B", "A", "C"])
    run_script("merge_measured.py", str(run), str(m))
    assert [r["stage"] for r in read(run / "op_table.csv")] == ["s1 a", "measured only X", "s1 b", "s2 a", "s2 c"]


def test_merge_divides_by_inferences(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, op_code="A")], [trace_row(1, 1, 1)])
    m = measured_seq(tmp_path / "m.csv", ["A", "X", "A", "X"])
    assert run_script("merge_measured.py", str(run), str(m), "--inferences", "2").returncode == 0
    assert read(run / "op_table.csv")[1]["p150:launches"] == "1"
    bad = measured_seq(tmp_path / "m3.csv", ["A", "X", "X", "X"])
    r = run_script("merge_measured.py", str(run), str(bad), "--inferences", "2")
    assert r.returncode == 2 and "not divisible" in r.stdout


def test_merge_with_blackhole_destination_validates(tmp_path):
    rows = [op_row(1, 1, op_code="A", **{"p100:shapes": "(1,32)", "p100:launches": "1", "p100:status": "✅",
                                        "p100:evidence": "CI"})]
    for col in ("quasar-port:port_op", "quasar-port:tests", "quasar-port:test_result",
                "quasar-port:status", "quasar-port:evidence"):
        del rows[0][col]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1), trace_row(2, 1, 1, profile="p100")],
                          destination="p100")
    run_script("merge_measured.py", str(run), str(measured_seq(tmp_path / "m.csv", ["A", "X"])))
    v = validate(run)
    assert v.returncode == 0, v.stdout


def test_kernel_ops_reads_measured_from_run_json(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, op_code="A")], [trace_row(1, 1, 1)])
    run_script("merge_measured.py", str(run), str(measured_seq(tmp_path / "m.csv", ["A", "A"])))
    run_script("kernel_ops.py", str(run))
    got = read(run / "kernel_ops.csv")[0]
    assert (got["origin"], got["measured_launches_op_code"]) == ("static+measured", "2")


def test_apply_does_not_pass_a_row_with_unrun_tests(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a; t.py::pass_b"])
    write_rows(run / "kernel_test_results.csv", [{"id": "1", "node_id": "t.py::pass_a", "op_table_ids": "1",
                                                  "result": "pass", "duration_s": "1", "log": "x"}])
    run_script("kernel_tests.py", "apply", str(run))
    got = read(run / "op_table.csv")[0]
    assert (got["quasar-port:test_result"], got["quasar-port:status"]) == ("not run", "⚠️")


def test_error_result_does_not_decide_status(tmp_path):
    run, _ = kernel_test_run(tmp_path, ["t.py::pass_a"])
    run_script("kernel_tests.py", "plan", str(run))
    r = run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(run.parent / "tt-metal"),
                   "--pytest-cmd", f"{sys.executable} -c pass")
    assert r.returncode == 0, r.stdout
    assert read(run / "kernel_test_results.csv")[0]["result"] == "error"
    run_script("kernel_tests.py", "apply", str(run))
    got = read(run / "op_table.csv")[0]
    assert (got["quasar-port:test_result"], got["quasar-port:status"]) == ("error", "⚠️")
    assert validate(run).returncode == 0


def test_kernel_tests_run_refuses_other_commit(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a"])
    data = json.loads((run / "run.json").read_text())
    data["tt_metal_commit"] = "0" * 40
    (run / "run.json").write_text(json.dumps(data))
    run_script("kernel_tests.py", "plan", str(run))
    r = run_script("kernel_tests.py", "run", str(run), "--tt-metal", str(run.parent / "tt-metal"), "--pytest-cmd", cmd)
    assert r.returncode == 1 and "0000000" in r.stdout


def test_kernel_tests_batches_keep_earlier_results(tmp_path):
    run, cmd = kernel_test_run(tmp_path, ["t.py::pass_a", "t.py::fail_b"])
    run_script("kernel_tests.py", "plan", str(run))
    env2 = tmp_path / "env2.json"
    env2.write_text(json.dumps({"TT_METAL_SIMULATOR": "/y/libttsim.so"}))
    tm = str(run.parent / "tt-metal")
    run_script("kernel_tests.py", "run", str(run), "--tt-metal", tm, "--pytest-cmd", cmd, "--only", "pass_a")
    run_script("kernel_tests.py", "run", str(run), "--tt-metal", tm, "--pytest-cmd", cmd, "--only", "fail_b",
               "--env-json", str(env2))
    got = {r["node_id"]: (r["result"], r["environment"]) for r in read(run / "kernel_test_results.csv")}
    assert got == {"t.py::pass_a": ("pass", "run.json"), "t.py::fail_b": ("fail", "env2.json")}


def test_node_ids_with_symbols_are_accepted(tmp_path):
    node = "tests/ttnn/x/test_a.py::test_b[H@g0.W@g1-(1, 2)-'x'-*]"
    out = validate(make_static_run(tmp_path, [op_row(1, 1, **{"quasar-port:tests": node})],
                                   [trace_row(1, 1, 1)])).stdout
    assert "not a list of pytest node ids" not in out
