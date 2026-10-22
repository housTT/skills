"""Reader-facing descriptions for the spreadsheet: one per tab (listed on Info) and one per column
(a note on its header cell).

A note says what a column holds and where its value comes from, in the words of
references/schemas.md, so a reader of the sheet needs no other document.
"""

from __future__ import annotations

STATIC, TRACY, GRAPH, CACHE, TTSIM = ("static trace", "Tracy", "graph capture", "kernel cache", "TTsim")

# Tab title (or prefix for tabs with a target suffix) -> (what it holds, the methods that fill it).
TAB_DESCRIPTIONS = {
    "Info": ("Run identity, bring-up checks, analysis methods and confidence, counts and the summary.", "all"),
    "Kernel ops": ("One row per kernel op (device op and program factory): launches, the methods that saw it, "
                   "and its status and evidence per target.", f"{STATIC}; {TRACY} and {GRAPH} confirm launches"),
    "Op table": ("One row per launching variant of a ttnn call: call site, dispatch, grid dependency, shapes and "
                 "status per target.", f"{STATIC}; {TRACY} adds measured-only rows"),
    "Kernel tests": ("Quasar kernel-op tests run on the simulator, one row per test with its result.", TTSIM),
    "Call trace": ("Launch order per model block, with repeats; its launch total must equal the Op table's.", STATIC),
    "Port blockers": ("What stops the code from running on a destination, written once and linked to the rows it "
                      "affects.", STATIC),
    "Host ops": ("Work the model does on the host, and whether it counts as host time inside the forward.",
                 STATIC),
    "Measured ops": ("Every op the profiled run executed, in order, with device and host time.", TRACY),
    "Host fallback": ("Device and host op counts and times in the profiled window.", TRACY),
    "Footprint": ("Launches and the largest core count per op code.", TRACY),
    "Static vs measured": ("Where the static trace and the Tracy run disagree.", f"{STATIC} and {TRACY}"),
    "Graph capture": ("One row per op code and program factory the captured run launched, with launches per "
                      "inference.", GRAPH),
    "Graph host transfers": ("Host transfers between the first and the last launch: next input upload, previous "
                             "readback, or host work inside the forward.", GRAPH),
    "Cache: compute APIs": ("Every distinct LLK call in the compute kernels the run compiled, with template "
                            "arguments, data formats and math config (the elfray Cache API Analyzer table).", CACHE),
    "Cache: dataflow APIs": ("Every distinct data-movement call in the run's kernels (elfray table).", CACHE),
    "Cache: kernels": ("One row per compiled kernel build, with errors if elfray could not read it.", CACHE),
    "LLK gaps (Quasar)": ("Each LLK API and data format of the cache compared with the Quasar LLK sources, "
                          "with the reviewed verdict per candidate.", f"{CACHE} and the Quasar sources"),
    "Changes": ("What changed in a table between two runs of the same model.", "comparison of two runs"),
}

STATUS_NOTE = ("✅ supported: a test or recorded run shows it. ⚠️ the code path exists but nothing tests it. "
               "❌ shown not to work. n/a: never launched on this target.")

# Notes by column field, shared by every tab unless a tab overrides it below.
FIELD_NOTES = {
    "id": "Row number. Other columns and tabs refer to rows by it.",
    "stage": "Model block, module and role of the call, from the model's structure.",
    "origin": "static: found by the static trace. measured: seen only in the Tracy run. static+measured: both. "
              "graph: launched in the graph capture but missing from the static trace.",
    "ttnn_api": "The ttnn call in the model code that launches this.",
    "op_code": "Device-operation class name, as Tracy and the graph capture report it.",
    "device_op": "The ttnn::prim device operation.",
    "program_factory": "The C++ program factory the device op selects for this call: the kernels that run.",
    "call_site": "Where the model makes the call, as file:line at the pinned tt-metal commit.",
    "launch_condition": "always, or the runtime condition under which the launch happens.",
    "confidence": "verified: the dispatch chain to this factory was followed in the code. unverified: not fully.",
    "dispatch_evidence": "The C++ file and function that select the device op and factory, and any runtime "
                         "alternative.",
    "grid_dependency": "Hardcoded grid, shard spec or program config this op depends on, with file:line. "
                       "automatic: derived from the device grid. none: no core grid. Hardcoded values block a "
                       "smaller grid.",
    "shapes": "Input and output shapes, dtype, layout and sharding on this target.",
    "launches": "Launches per inference on this target.",
    "status": STATUS_NOTE,
    "evidence": "The decisive fact behind this target's status, with file:line.",
    "tests": "Quasar kernel-op tests (pytest node ids) that decide the status.",
    "test_result": "Simulator result of those tests: pass, fail, xfail, timeout, error, skip, or not run.",
    "statuses": "Number of op-table rows with each status. " + STATUS_NOTE,
    "test_results": "Simulator result of each kernel-op test.",
    "sources": "Methods that saw this kernel op. tracy (op code): Tracy saw the op code, which several kernel ops "
               "share, so it does not confirm this factory.",
    "op_table_ids": "The Op table rows this kernel op covers.",
    "static_launches": "Launches per inference of these rows, from the static trace.",
    "static_launches_op_code": "Static launches of every factory of this op code.",
    "measured_launches_op_code": "Launches per inference of this op code in the Tracy run (Tracy does not name "
                                 "factories).",
    "graph_launches": "Launches per inference of this device op and factory in the graph capture.",
    "profile": "Target the launches are counted for.",
    "ops": "Device ops of the block, in launch order.",
    "repeats": "How many times the block runs per inference.",
    "launches_per_repeat": "Launches per run of the block.",
    "notes": "What the trace decided here and why.",
    "blocker": "What stops the code from running on the destination.",
    "affected_ids": "Op table rows the blocker affects.",
    "required_change": "What the port has to change.",
    "host_work": "Work done on the host.",
    "when": "When and how often it runs.",
    "counts_as_host_op": "yes: runs on the host once per inference inside the forward, so it adds to host time.",
    "reason": "Why it counts or not.",
    "op_type": "tt_dnn_device for a device op; tt_dnn_cpu or python_fallback for host work.",
    "attributes": "Op attributes Tracy recorded (program config and others).",
    "inputs": "Input tensors: shape, layout, dtype, memory.",
    "outputs": "Output tensors: shape, layout, dtype, memory.",
    "core_count": "Cores the op ran on.",
    "device_kernel_ns": "Device kernel duration, ns.",
    "host_ns": "Host duration, ns.",
    "scope": "What the row summarizes.",
    "window": "warm: between signposts, compiled kernels; cold: whole test, compilation included.",
    "device_ops": "Device ops in the window.",
    "host_ops": "Host ops in the window.",
    "device_op_time_ms": "Total device kernel time, ms.",
    "host_op_time_ms": "Total host op time, ms.",
    "total_time_ms": "Wall time of the window, ms.",
    "host_pct_of_total": "Host op time as a share of the window's wall time.",
    "max_core_count": "Largest core count any launch of this op code used.",
    "peak_dram_mb": "Peak DRAM, only when measured.",
    "category": "Kind of disagreement.",
    "measured_launches": "Launches the Tracy run shows.",
    "static_ids": "Op table rows of the static side.",
    "detail": "What differs.",
    "program_factory_type": "Full C++ type of the factory the graph capture recorded.",
    "ttnn_apis": "Top-level ttnn calls that launched it.",
    "launches_per_inference": "Launches divided by the forwards the test dispatched.",
    "cache_misses": "Launches that built a new program (the first of each configuration).",
    "transfer": "to_torch, from_torch, from_device, to_device, copy_host_to_device or copy_device_to_host.",
    "after_op_code": "The launch just before the transfer.",
    "before_op_code": "The launch just after the transfer.",
    "count": "How many times this transfer happened between these launches.",
    "node_id": "Pytest node id of the kernel-op test.",
    "result": "Simulator result: pass, fail, xfail, timeout, error or skip.",
    "duration_s": "Run time, s.",
    "log": "Path of the pytest log.",
    "environment": "The environment the test ran under.",
    "change": "added, removed or changed.",
    "key": "Row identity the runs are matched on.",
    "column": "Changed column.",
    "old": "Value in the earlier run.",
    "new": "Value in the later run.",
    "API": "API name with the template arguments it was instantiated with.",
    "Kernels": "Kernels that make this call.",
    "TTNN Ops": "The enclosing compute-API call (for example pack_tile); elfray's column name, not a ttnn op.",
    "Op Args": "Runtime arguments: a constant value, or =? when the value is only known at run time.",
    "Input Data Formats": "Data format per input circular buffer (cbN=format).",
    "Output Data Formats": "Data format per output circular buffer.",
    "Tile Dims": "Tile shape per circular buffer.",
    "Math Fidelity": "Math fidelity the kernel was compiled with.",
    "Math Approx": "Approximate math mode.",
    "FP32 Dest Accum": "Whether the destination register accumulates in FP32.",
    "Dst Sync Mode": "Destination register sync mode.",
    "Layer": "dataflow_api (user-facing) or noc_api (lower-level NOC helpers).",
    "Processor(s)": "Data-movement processors that make the call (brisc, ncrisc).",
    "Enclosing Dataflow API": "The dataflow API call this one is inlined into.",
    "kernel": "Kernel name (its source file).",
    "compile_hash": "Compile hash: one per distinct compile-time configuration of the kernel.",
    "api_calls": "API calls read from this build in the selected layers.",
    "errors": "Why elfray could not read part of the build.",
    "name": "LLK API or data format name.",
    "quasar_status": "Found by name in the Quasar sources: present, signature differs, missing; not an LLK for "
                     "other calls.",
    "review": "Verdict after reading the Quasar headers: gap, renamed or not a gap. Empty: not reviewed yet.",
    "review_note": "The Quasar source that decides the verdict.",
    "template_args": "Template arguments the kernels instantiated.",
    "missing_template_args": "Those the closest Quasar definition does not take.",
    "quasar_template_args": "Template arguments of the closest Quasar definition.",
    "missing_runtime_args": "Runtime arguments the closest Quasar definition does not take.",
    "similar_quasar_apis": "Quasar LLK names sharing the longest prefix, for an API not found by name.",
    "quasar_definitions": "file:line of each Quasar definition.",
    "configs": "Distinct template configurations the cache holds.",
    "cache_rows": "Cache table rows that use it.",
    "kernels": "Kernels that use it.",
}

# Fields whose meaning differs on one tab.
TAB_FIELD_NOTES = {
    "Graph capture": {"launches": "Launches in the whole capture."},
    "Footprint": {"launches": "Launches of this op code in the Tracy window."},
    "Cache: kernels": {"kind": "compute or data_movement."},
    "LLK gaps (Quasar)": {"kind": "llk_api or data_format."},
}


def tab_description(title: str) -> tuple[str, str] | None:
    for prefix, description in TAB_DESCRIPTIONS.items():
        if title == prefix or title.startswith(prefix + " (") or (prefix == "Changes" and title.startswith("Changes: ")):
            return description
    return None


def column_note(tab: str, column: str) -> str:
    """Note for a CSV column (a field, or `<target>:<field>`) on the given tab."""
    field = column.split(":", 1)[-1]
    overrides = TAB_FIELD_NOTES.get(tab) or TAB_FIELD_NOTES.get(tab.split(" (")[0], {})
    return overrides.get(field) or FIELD_NOTES.get(field, "")
