"""Decompose a tt-metal kernel cache into the APIs each kernel calls, using the elfray engine.

Writes cache_compute.csv and cache_dataflow.csv with the columns of the elfray VS Code extension's
Cache API Analyzer, built the same way, plus cache_kernels.csv (one row per kernel build). Records
the cache in run.json `kernel_cache`.

The engine (`elfray --inlined-calls`, `elfray --enum`) reads each kernel ELF's DWARF; this script
does what the extension's frontend does on top: find the kernel builds, classify each inlined call
into an API layer, attach the enclosing compute or dataflow API call, read data formats, tile dims
and math config from chlkc_descriptors.h, and collapse identical calls across kernels.

The cache must come from one run of the entry test with TT_METAL_CACHE pointing at an empty
directory and TT_METAL_RISCV_DEBUG_INFO=1; otherwise it mixes in other tests' kernels or carries
no DWARF.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import (  # noqa: E402
    CACHE_COMPUTE_COLUMNS, CACHE_DATAFLOW_COLUMNS, CACHE_KERNELS_COLUMNS, write_csv,
)

INSTALL_HINT = ("install it with: gh api repos/tenstorrent/tt-elfray/contents/install.sh "
                "-H 'Accept: application/vnd.github.raw' | bash  (Linux x86_64; puts elfray in ~/.local/bin)")
# The VS Code extension bundles the engine under bin/<target>/ in its install directory.
EXTENSION_GLOBS = ("~/.vscode-server/extensions/elfray.elfray-*/bin/*/elfray*",
                   "~/.vscode/extensions/elfray.elfray-*/bin/*/elfray*",
                   "~/.cursor-server/extensions/elfray.elfray-*/bin/*/elfray*")

LAYERS = ("compute_api", "llk_api", "llk_core", "dataflow_api", "noc_api", "other")
DEFAULT_LAYERS = ("llk_api", "dataflow_api")
LAYER_ORDER = {layer: n for n, layer in enumerate(("compute_api", "llk_api", "llk_core", "dataflow_api", "noc_api",
                                                   "other"))}
THREADS = ("unpack", "math", "pack")
THREAD_ORDER = {t: n for n, t in enumerate((*THREADS, "brisc", "ncrisc"))}
DM_PROCESSORS = ("brisc", "ncrisc")

COMPUTE_API_PATH_HINTS = ("/api/compute/", "compute_kernel_api")
DATAFLOW_API_PATH_HINTS = ("/api/dataflow/", "dataflow_api")
NOC_API_PATH_HINTS = ("noc_nonblocking_api", "/noc/")
NOC_API_NAME_PREFIXES = ("ncrisc_noc_", "noc_cmd_buf_", "noc_fast_")
DATAFLOW_API_NAME_PREFIXES = ("cb_", "noc_async_", "noc_semaphore_", "noc_inline_", "get_")

UNUSED_FORMAT_CODE = 255
ARRAY_RE = re.compile(r"constexpr\s+(?:[A-Za-z_][A-Za-z0-9_:]*\s+)+(\w+)\s*\[\d+\]\s*=\s*\{([^}]*)\}")
SCALAR_BOOL_RE = re.compile(r"constexpr\s+bool\s+(\w+)\s*=\s*(true|false)\s*;")
DST_SYNC_RE = re.compile(r"#define\s+DST_SYNC_MODE\s+DstSync::(\w+)")
MATH_FIDELITY_RE = re.compile(r"MATH_FIDELITY\s*=\s*static_cast<ckernel::MathFidelity>\((\d+)\)")
MATH_FIDELITY_NAMES = {0: "LoFi", 2: "HiFi2", 3: "HiFi3", 4: "HiFi4"}
# Runtime-arg names that carry an output CB index; names with "tile" are offsets, not CBs.
OUTPUT_CB_ARG_SUBSTRINGS = ("output", "ocb")
OUTPUT_CB_ARGS = {"out_cb"}


# ---- engine -------------------------------------------------------------------------------------

def _is_engine(path: str) -> bool:
    try:
        out = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--inlined-calls" in out.stdout + out.stderr


def find_elfray(explicit: str | None) -> str | None:
    candidates = [explicit] if explicit else []
    candidates += [os.environ.get("ELFRAY"), os.environ.get("ELFRAY_BIN"), shutil.which("elfray"),
                   os.path.expanduser("~/.local/bin/elfray")]
    for pattern in EXTENSION_GLOBS:
        candidates += sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK) and _is_engine(path):
            return path
        if explicit and path == explicit:
            return None
    return None


def engine_json(elfray: str, args: list[str]) -> dict:
    out = subprocess.run([elfray, *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"elfray exited {out.returncode}")
    return json.loads(out.stdout)


# ---- discovery ----------------------------------------------------------------------------------

def _elfs(directory: pathlib.Path) -> tuple[dict[int, pathlib.Path], dict[str, pathlib.Path]]:
    trisc = {n: directory / f"trisc{n}" / f"trisc{n}.elf" for n in range(3)}
    dm = {p: directory / p / f"{p}.elf" for p in DM_PROCESSORS}
    return ({n: p for n, p in trisc.items() if p.is_file()}, {k: p for k, p in dm.items() if p.is_file()})


def discover(root: pathlib.Path) -> list[dict]:
    """Every kernel build directory under root (a cache root, build key, kernel or hash dir)."""
    builds = []

    def visit(directory: pathlib.Path) -> None:
        trisc, dm = _elfs(directory)
        if trisc or dm:
            parent = directory.parent
            in_kernels = parent.name == "kernels"
            builds.append({
                "name": directory.name if in_kernels else parent.name,
                "compile_hash": "" if in_kernels else directory.name,
                "path": directory, "kind": "compute" if trisc else "data_movement",
                "trisc": trisc, "dm": dm,
            })
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name != "firmware":
                visit(child)

    visit(root)
    return builds


# ---- classification ------------------------------------------------------------------------------

def classify(name: str, source_file: str | None, operation_layer: str) -> str:
    source = (source_file or "").replace("\\", "/")
    if operation_layer == "compute_api":
        if name.startswith("_llk_"):
            return "llk_core"
        if name.startswith("llk_"):
            return "llk_api"
        if source and any(h in source for h in COMPUTE_API_PATH_HINTS):
            return "compute_api"
        return "other"
    if source:
        if any(h in source for h in NOC_API_PATH_HINTS):
            return "noc_api"
        if any(h in source for h in DATAFLOW_API_PATH_HINTS):
            return "dataflow_api"
    if name.startswith(NOC_API_NAME_PREFIXES):
        return "noc_api"
    if name.startswith(DATAFLOW_API_NAME_PREFIXES):
        return "dataflow_api"
    return "other"


def calls_from_function(fn: dict, thread: str, include: set[str], operation_layer: str) -> list[dict]:
    """Classified calls of one host function, each with its nearest enclosing operation-layer call.

    The engine lists inlined calls in DWARF pre-order with their nesting depth, so the enclosing
    call is the latest operation-layer call at a smaller depth.
    """
    out, op_stack = [], []
    for raw in fn.get("calls", []):
        while op_stack and op_stack[-1][0] >= raw["depth"]:
            op_stack.pop()
        enclosing = op_stack[-1][1] if op_stack else None
        name = raw.get("name", "")
        layer = classify(name, raw.get("source_file"), operation_layer)
        if layer == operation_layer and name:
            op_stack.append((raw["depth"], name))
        if not name or layer not in include:
            continue
        out.append({"name": name, "layer": layer, "thread": thread, "template_args": raw.get("template_args", []),
                    "runtime_args": raw.get("runtime_args", []), "operation": enclosing,
                    "call_line": raw.get("call_line") or 0, "call_column": raw.get("call_column") or 0})
    return out


# ---- descriptors ---------------------------------------------------------------------------------

def _int_array(text: str) -> list[int]:
    return [int(t) for t in (x.strip() for x in text.replace("\n", "").split(",")) if t]


def parse_descriptors(source: str, format_names: dict[int, str]) -> dict:
    arrays = {m.group(1): _int_array(m.group(2)) for m in ARRAY_RE.finditer(source)}
    if "unpack_src_format" not in arrays or "pack_dst_format" not in arrays:
        raise ValueError("missing unpack_src_format/pack_dst_format; not a recognizable chlkc_descriptors.h")
    bools = {m.group(1): m.group(2) == "true" for m in SCALAR_BOOL_RE.finditer(source)}
    sync = DST_SYNC_RE.search(source)
    fidelity = MATH_FIDELITY_RE.search(source)

    def cbs(prefix: str, format_array: str) -> list[dict]:
        rows, tile_r, tile_c = [], arrays.get(f"{prefix}_tile_r_dim", []), arrays.get(f"{prefix}_tile_c_dim", [])
        for index, code in enumerate(arrays[format_array]):
            if code == UNUSED_FORMAT_CODE:
                continue
            rows.append({"index": index, "data_format": format_names.get(code, f"Unknown({code})"),
                         "tile_r_dim": tile_r[index] if index < len(tile_r) else None,
                         "tile_c_dim": tile_c[index] if index < len(tile_c) else None})
        return rows

    code = int(fidelity.group(1)) if fidelity else None
    return {
        "dst_accum_mode": bools.get("DST_ACCUM_MODE"), "approx_mode": bools.get("APPROX"),
        "dst_sync_mode": sync.group(1) if sync else None,
        "math_fidelity": None if code is None else MATH_FIDELITY_NAMES.get(code, f"Fidelity({code})"),
        # Both directions use the L1 format: unpack reads its input CB, pack writes its output CB.
        "unpack_inputs": cbs("unpack", "unpack_src_format"), "pack_outputs": cbs("pack", "pack_dst_format"),
    }


# ---- per-kernel analysis -------------------------------------------------------------------------

def analyze_kernel(elfray: str, build: dict, include: set[str]) -> dict:
    kernel = {**build, "calls": [], "errors": [], "descriptors": None}
    format_names: dict[int, str] = {}
    elfs = [(elf, THREADS[n], "compute_api", True) for n, elf in sorted(build["trisc"].items())]
    elfs += [(elf, p, "dataflow_api", False) for p, elf in sorted(build["dm"].items())]
    for elf, thread, operation_layer, read_formats in elfs:
        try:
            doc = engine_json(elfray, ["--inlined-calls", str(elf)])
            if not doc.get("dwarf"):
                raise RuntimeError("no DWARF debug info (rebuild with TT_METAL_RISCV_DEBUG_INFO=1)")
            for fn in doc.get("functions", []):
                kernel["calls"] += calls_from_function(fn, thread, include, operation_layer)
            if read_formats and not format_names:
                enum = engine_json(elfray, ["--enum", "DataFormat", str(elf)])
                for value in enum.get("values", []):
                    format_names.setdefault(value["value"], value["name"])
        except (RuntimeError, ValueError, OSError) as exc:
            kernel["errors"].append(f"{thread} ({elf.name}): {exc}")
    header = build["path"] / "chlkc_descriptors.h"
    if build["kind"] == "compute":
        if header.is_file():
            try:
                kernel["descriptors"] = parse_descriptors(header.read_text(encoding="utf-8"), format_names)
            except ValueError as exc:
                kernel["errors"].append(f"descriptor parse failed: {exc}")
        else:
            kernel["errors"].append("chlkc_descriptors.h not found beside the ELFs; formats and math config unknown")
    return kernel


# ---- display and collapse --------------------------------------------------------------------------

def header(call: dict) -> str:
    args = call["template_args"]
    if not args:
        return call["name"]
    inner = ", ".join(f"{t['name']}={t['display']}" for t in args)
    return f"{call['name']}<{inner}>"


def _int_set(values: list[int]) -> str:
    ordered = sorted(set(values))
    parts, start, prev = [], ordered[0], ordered[0]
    for v in ordered[1:]:
        if v == prev + 1:
            prev = v
            continue
        parts.append(str(start) if start == prev else f"{start}..{prev}")
        start = prev = v
    parts.append(str(start) if start == prev else f"{start}..{prev}")
    return ",".join(parts)


def runtime_arg(arg: dict) -> str:
    if not arg["is_static"]:
        return arg["name"]
    values = arg["static_values"]
    if len(values) == 1:
        return f"{arg['name']}={values[0]}"
    return f"{arg['name']}={{{_int_set(values)}}}"


def op_args(call: dict) -> str:
    if not call["runtime_args"]:
        return "-"
    return ", ".join(runtime_arg(a) if a["is_static"] else f"{a['name']}=?" for a in call["runtime_args"])


def _signature(call: dict) -> str:
    return json.dumps([call["name"], call["layer"], [[t["name"], t["display"]] for t in call["template_args"]]])


def _instance(call: dict) -> str:
    static = [[a["name"], a["static_values"]] for a in call["runtime_args"] if a["is_static"]]
    return _signature(call) + "|" + json.dumps(static)


def unique_calls(kernel: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    for call in kernel["calls"]:
        seen.setdefault(_instance(call) + "|" + call["thread"], call)
    return sorted(seen.values(), key=lambda c: (LAYER_ORDER.get(c["layer"], 99), THREAD_ORDER.get(c["thread"], 99),
                                                c["name"], header(c), c["call_line"], c["call_column"]))


def _is_output_cb_arg(name: str) -> bool:
    return "tile" not in name and (name in OUTPUT_CB_ARGS or any(s in name for s in OUTPUT_CB_ARG_SUBSTRINGS))


def _call_cbs(call: dict) -> tuple[set[int], set[int]]:
    inputs, outputs = set(), set()
    for arg in call["runtime_args"]:
        if not arg["is_static"]:
            continue
        if "operand" in arg["name"]:
            inputs.update(arg["static_values"])
        elif _is_output_cb_arg(arg["name"]):
            outputs.update(arg["static_values"])
    return inputs, outputs


def _resolve_cbs(call: dict, kernel: dict, configured) -> tuple[set[int], set[int]]:
    """The call's own CB args, else every CB the kernel's calls name, else every configured CB."""
    inputs, outputs = _call_cbs(call)
    if inputs or outputs:
        return inputs, outputs
    inputs, outputs = set(), set()
    for other in kernel["calls"]:
        i, o = _call_cbs(other)
        inputs |= i
        outputs |= o
    if inputs or outputs:
        return inputs, outputs
    return set(configured), set(configured)


def _cb_column(values: dict[int, str], cbs: set[int]) -> str:
    indices = sorted(i for i in cbs if i in values)
    return ", ".join(f"cb{i}={values[i]}" for i in indices) if indices else "-"


def _tri(value) -> str:
    return "-" if value is None else ("true" if value else "false")


def compute_rows(kernels: list[dict]) -> list[list[str]]:
    merged: dict[str, dict] = {}
    for kernel in kernels:
        if kernel["kind"] != "compute":
            continue
        desc = kernel["descriptors"] or {}
        formats, tiles = {}, {}
        for cb in desc.get("unpack_inputs", []) + desc.get("pack_outputs", []):
            formats.setdefault(cb["index"], cb["data_format"])
            if cb["tile_r_dim"] is not None and cb["tile_c_dim"] is not None:
                tiles.setdefault(cb["index"], f"{cb['tile_r_dim']}x{cb['tile_c_dim']}")
        config = [desc.get("math_fidelity") or "-", _tri(desc.get("approx_mode")),
                  _tri(desc.get("dst_accum_mode")), desc.get("dst_sync_mode") or "-"]
        for call in unique_calls(kernel):
            in_cbs, out_cbs = _resolve_cbs(call, kernel, formats.keys())
            tile_in, tile_out = _resolve_cbs(call, kernel, tiles.keys())
            cells = [header(call), op_args(call), _cb_column(formats, in_cbs), _cb_column(formats, out_cbs),
                     _cb_column(tiles, tile_in | tile_out), *config]
            entry = merged.setdefault(json.dumps(cells), {"cells": cells, "kernels": set(), "ops": set()})
            entry["kernels"].add(kernel["name"])
            if call["operation"]:
                entry["ops"].add(call["operation"])
    rows = [[e["cells"][0], ", ".join(sorted(e["kernels"])), ", ".join(sorted(e["ops"])) or "-", *e["cells"][1:]]
            for e in merged.values()]
    return sorted(rows, key=lambda r: (r[0], r[3], r[4]))


def dataflow_rows(kernels: list[dict]) -> list[list[str]]:
    merged: dict[str, dict] = {}
    for kernel in kernels:
        if kernel["kind"] != "data_movement":
            continue
        # Every call, not unique_calls: the same NOC call under different dataflow APIs stays apart.
        for call in kernel["calls"]:
            cells = [header(call), call["layer"], op_args(call), call["operation"] or "-"]
            entry = merged.setdefault(json.dumps(cells), {"cells": cells, "kernels": set(), "procs": set()})
            entry["kernels"].add(kernel["name"])
            entry["procs"].add(call["thread"])
    rows = [[e["cells"][0], ", ".join(sorted(e["kernels"])), e["cells"][1], ", ".join(sorted(e["procs"])),
             e["cells"][2], e["cells"][3]] for e in merged.values()]
    return sorted(rows, key=lambda r: (r[0], r[4], r[2]))


def _table(columns: tuple[str, ...], rows: list[list[str]]) -> list[dict]:
    return [dict(zip(columns, [str(n), *row])) for n, row in enumerate(rows, 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=pathlib.Path, help="TT_METAL_CACHE of the entry-test run (or a subdirectory)")
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("--elfray", help="path to the elfray engine; searched for when omitted")
    parser.add_argument("--layers", default=",".join(DEFAULT_LAYERS),
                        help=f"comma-separated API layers to collect, from {', '.join(LAYERS)}")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--date", default=datetime.date.today().isoformat(), help="date of the run that built the cache")
    args = parser.parse_args()
    include = {x.strip() for x in args.layers.split(",") if x.strip()}
    unknown = include - set(LAYERS)
    if unknown:
        print(f"ERROR: unknown layers {sorted(unknown)}; choose from {', '.join(LAYERS)}")
        return 2
    elfray = find_elfray(args.elfray)
    if not elfray:
        where = (f"{args.elfray} is not an elfray engine" if args.elfray
                 else "searched $ELFRAY, $ELFRAY_BIN, PATH, ~/.local/bin and the VS Code extension")
        print(f"ERROR: no elfray engine ({where}); {INSTALL_HINT}")
        return 3
    if not args.cache.is_dir():
        print(f"ERROR: {args.cache} is not a directory")
        return 2
    builds = discover(args.cache)
    if not builds:
        print(f"ERROR: no kernel ELFs under {args.cache}")
        return 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        kernels = list(pool.map(lambda b: analyze_kernel(elfray, b, include), builds))

    if all(not k["calls"] for k in kernels):
        print("ERROR: no API call recovered from any kernel; the cache was built without "
              "TT_METAL_RISCV_DEBUG_INFO=1 or the layer selection matches nothing")
        for kernel in kernels[:5]:
            print(f"  {kernel['name']}: {'; '.join(kernel['errors']) or 'no calls in the selected layers'}")
        return 2
    compute = compute_rows(kernels)
    dataflow = dataflow_rows(kernels)
    write_csv(args.run_dir / "cache_compute.csv", CACHE_COMPUTE_COLUMNS, _table(CACHE_COMPUTE_COLUMNS, compute))
    write_csv(args.run_dir / "cache_dataflow.csv", CACHE_DATAFLOW_COLUMNS, _table(CACHE_DATAFLOW_COLUMNS, dataflow))
    write_csv(args.run_dir / "cache_kernels.csv", CACHE_KERNELS_COLUMNS, [
        {"id": str(n), "kernel": k["name"], "kind": k["kind"], "compile_hash": k["compile_hash"],
         "api_calls": str(len(k["calls"])), "errors": "; ".join(k["errors"])} for n, k in enumerate(kernels, 1)])

    failed = [k for k in kernels if k["errors"]]
    run_path = args.run_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["kernel_cache"] = {
        "path": str(args.cache.resolve()), "date": args.date, "elfray": elfray, "layers": sorted(include),
        "kernel_builds": len(kernels), "compute_builds": sum(k["kind"] == "compute" for k in kernels),
        "dataflow_builds": sum(k["kind"] == "data_movement" for k in kernels),
        "compute_rows": len(compute), "dataflow_rows": len(dataflow), "builds_with_errors": len(failed),
    }
    run_path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(kernels)} kernel builds ({run['kernel_cache']['compute_builds']} compute, "
          f"{run['kernel_cache']['dataflow_builds']} data movement): {len(compute)} compute rows, "
          f"{len(dataflow)} dataflow rows, engine {elfray}")
    for kernel in failed:
        print(f"WARNING: {kernel['name']} {kernel['compile_hash']}: {'; '.join(kernel['errors'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
