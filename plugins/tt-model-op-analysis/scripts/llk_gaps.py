"""First-pass Quasar LLK gap check of a kernel cache's compute APIs, at a pinned tt-metal commit.

Reads a compute table (cache_compute.csv, or the elfray extension's compute CSV export) and, for
each LLK API the kernels call, looks for its definitions in the Quasar LLK sources:
tt_metal/hw/ckernels/quasar (llk_* API) and tt_metal/tt-llk/tt_llk_quasar (_llk_* core). For each
it reports:

- `present`: a Quasar overload takes every template argument the kernels instantiated;
- `signature differs`: Quasar defines the API, but no overload takes all of those template
  arguments (the missing names are listed);
- `missing`: no Quasar definition.

It also checks every data format in the table against the Quasar DataFormat enum.

The match is by name, read from source text: an API can exist under another name or behind a
wrapper (Quasar has llk_math_matmul_tile and _block, no llk_math_matmul), and a template argument
can be renamed (is_fp32_dest_acc_en is EN_32BIT_DEST on Quasar). Every `missing` or `signature
differs` row is a candidate, confirmed only by reading the Quasar headers; `quasar_template_args`
and `similar_quasar_apis` point at what to read.

`review` records the verdict after reading them: `gap` (Quasar lacks it), `renamed` (Quasar has it
under the name or arguments given in the note) or `not a gap`. A new check keeps a verdict while
the finding it was made on (status, missing arguments, Quasar definitions) is unchanged.

    llk_gaps.py check <compute csv> --tt-metal <checkout> (--run-dir <run> | --ref <sha> --out <csv>)
    llk_gaps.py review <gaps csv> <id> <verdict> --note "<Quasar file:line and what it shows>"
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import LLK_GAP_VERDICTS, LLK_GAPS_COLUMNS, read_csv, write_csv  # noqa: E402

QUASAR_DIRS = ("tt_metal/hw/ckernels/quasar", "tt_metal/tt-llk/tt_llk_quasar")
QUASAR_TYPES = "tt_metal/hw/inc/internal/tt-2xx/quasar/tensix_types.h"
SOURCE_SUFFIXES = (".h", ".hpp", ".cc", ".cpp")
NOT_A_TYPE = {"return", "else", "if", "while", "for", "switch", "case", "do", "new", "delete", "throw"}
# Upper-case tokens are inline/attribute macros (ALWI, TT_ALWAYS_INLINE, FORCE_INLINE).
SPECIFIERS = (r"(?:inline|static|constexpr|const|volatile|[A-Z][A-Z0-9_]{2,}|__attribute__\s*\(\([^)]*\)\)"
              r"|\[\[[^\]]*\]\])")


# ---- the compute table ---------------------------------------------------------------------------

def split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def parse_api(cell: str) -> tuple[str, list[str]]:
    """`name<a=x, b=y>` (backticks optional) -> (name, [a, b])."""
    text = cell.strip().strip("`")
    if "<" not in text or not text.endswith(">"):
        return text, []
    name, inner = text.split("<", 1)
    return name.strip(), [p.split("=", 1)[0].strip() for p in split_top(inner[:-1])]


def arg_names(cell: str) -> list[str]:
    """Runtime argument names of an `Op Args` cell (`a=1, b=?`, `a={0..3}`), `-` for none."""
    if cell.strip() in ("", "-"):
        return []
    return [p.split("=", 1)[0].strip() for p in split_top(cell)]


def formats(cell: str) -> list[str]:
    return [p.split("=", 1)[1] for p in (x.strip() for x in cell.split(",")) if "=" in p]


def collect(rows: list[dict]) -> tuple[dict, dict]:
    """Per API name: template-arg names, runtime-arg names, configs, kernels, rows. Per format: rows."""
    apis: dict[str, dict] = {}
    fmt_rows: dict[str, dict] = {}
    for row in rows:
        name, targs = parse_api(row["API"])
        entry = apis.setdefault(name, {"targs": [], "rargs": [], "configs": set(), "kernels": set(), "rows": 0})
        entry["targs"] += [t for t in targs if t not in entry["targs"]]
        entry["rargs"] += [a for a in arg_names(row.get("Op Args", "")) if a not in entry["rargs"]]
        entry["configs"].add(row["API"].strip().strip("`"))
        entry["kernels"].update(k.strip() for k in row.get("Kernels", "").split(",") if k.strip())
        entry["rows"] += 1
        row_formats = {fmt for column in ("Input Data Formats", "Output Data Formats")
                       for fmt in formats(row.get(column, ""))}
        for fmt in row_formats:
            f = fmt_rows.setdefault(fmt, {"kernels": set(), "rows": 0})
            f["kernels"].update(k.strip() for k in row.get("Kernels", "").split(",") if k.strip())
            f["rows"] += 1
    return apis, fmt_rows


# ---- the Quasar sources --------------------------------------------------------------------------

def git(checkout: pathlib.Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def quasar_sources(checkout: pathlib.Path, ref: str) -> dict[str, str]:
    """Quasar LLK sources at `ref`, comments blanked (line numbers kept)."""
    paths = [p for p in git(checkout, "ls-tree", "-r", "--name-only", ref, "--", *QUASAR_DIRS).splitlines()
             if p.endswith(SOURCE_SUFFIXES)]
    absent = [d for d in QUASAR_DIRS if not any(p.startswith(d + "/") for p in paths)]
    if absent:
        # Older commits keep tt-llk as a submodule or have no Quasar API; every API would read as missing.
        raise RuntimeError(f"{', '.join(absent)} has no sources at {ref[:12]}; this commit cannot be checked")
    return {p: strip_comments(git(checkout, "show", f"{ref}:{p}")) for p in paths}


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)) if "\n" not in m.group(0)
                  else "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket that closes the one at `start`, or -1."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _param_names(text: str) -> list[str]:
    names = []
    for param in split_top(text):
        param = re.sub(r"\[\[[^\]]*\]\]", "", split_top(param, "=")[0] if "=" in param else param).strip()
        match = re.search(r"(\w+)\s*(?:\[[^\]]*\])?$", param)
        if match and match.group(1) not in ("void", "typename", "class"):
            names.append(match.group(1))
    return names


def definitions(name: str, sources: dict[str, str]) -> list[dict]:
    """Every definition of `name` with its template and function parameter names."""
    pattern = re.compile(r"(?m)^[ \t]*(?:" + SPECIFIERS + r"\s+)*([\w:]+(?:<[^;{}()]*>)?)[\s*&]+" + re.escape(name)
                         + r"\s*\(")
    found = []
    for path, text in sources.items():
        for match in pattern.finditer(text):
            if match.group(1) in NOT_A_TYPE:
                continue
            paren = match.end() - 1
            close = _balanced(text, paren, "(", ")")
            if close < 0:
                continue
            after = text[close:close + 200].lstrip()
            # A declaration ends in ';'; keep it only when it is the sole signature in the file.
            body = after.startswith(("{", "noexcept", "const", "->"))
            template = []
            head = text[:match.start()]
            t = head.rfind("template")
            if t >= 0:
                lt = head.find("<", t)
                end = _balanced(head, lt, "<", ">") if lt >= 0 else -1
                if end > 0 and not head[end:].strip(" \t\n"):
                    template = _param_names(head[lt + 1:end - 1])
            found.append({"where": f"{path}:{text.count(chr(10), 0, match.start()) + 1}", "template": template,
                          "params": _param_names(text[paren + 1:close - 1]), "body": body})
    with_body = [d for d in found if d["body"]]
    return with_body or found


LLK_NAME = re.compile(r"\b(_?llk_\w+)\s*(?:<[^;{}()]*>)?\s*\(")


def quasar_llk_names(sources: dict[str, str]) -> set[str]:
    """Names of the LLK functions the Quasar sources mention, defined or called."""
    return {m.group(1) for text in sources.values() for m in LLK_NAME.finditer(text)}


def similar(name: str, known: set[str], limit: int = 6) -> list[str]:
    """Quasar LLK names sharing the longest underscore-separated prefix with `name`."""
    parts = name.split("_")
    for cut in range(len(parts), 2, -1):
        stem = "_".join(parts[:cut])
        hits = sorted(k for k in known if k != name and (k.startswith(stem + "_") or k == stem))
        if hits:
            return hits[:limit]
    return []


def quasar_formats(checkout: pathlib.Path, ref: str) -> set[str]:
    text = strip_comments(git(checkout, "show", f"{ref}:{QUASAR_TYPES}"))
    match = re.search(r"enum\s+class\s+DataFormat[^{]*\{(.*?)\};", text, re.S)
    if not match:
        raise RuntimeError(f"no DataFormat enum in {QUASAR_TYPES}")
    return {m.group(1) for m in re.finditer(r"(\w+)\s*=", match.group(1))}


# ---- report --------------------------------------------------------------------------------------

def check(apis: dict, fmt_rows: dict, sources: dict[str, str], known_formats: set[str]) -> list[dict]:
    out = []
    known_names = quasar_llk_names(sources)
    for name in sorted(apis):
        entry = apis[name]
        row = {"kind": "llk_api", "name": name, "configs": str(len(entry["configs"])),
               "template_args": ", ".join(entry["targs"]), "kernels": ", ".join(sorted(entry["kernels"])),
               "cache_rows": str(entry["rows"]), "quasar_definitions": "", "missing_template_args": "",
               "quasar_template_args": "", "missing_runtime_args": "", "similar_quasar_apis": ""}
        if not name.startswith(("llk_", "_llk_")):
            out.append(row | {"quasar_status": "not an LLK"})
            continue
        defs = definitions(name, sources)
        if not defs:
            out.append(row | {"quasar_status": "missing",
                              "similar_quasar_apis": ", ".join(similar(name, known_names))})
            continue
        best = max(defs, key=lambda d: (len(set(entry["targs"]) & set(d["template"])),
                                        len(set(entry["rargs"]) & set(d["params"]))))
        missing_t = [t for t in entry["targs"] if t not in best["template"]]
        row["quasar_definitions"] = "; ".join(d["where"] for d in defs)
        row["missing_template_args"] = ", ".join(missing_t)
        row["quasar_template_args"] = ", ".join(best["template"])
        row["missing_runtime_args"] = ", ".join(a for a in entry["rargs"] if a not in best["params"])
        out.append(row | {"quasar_status": "signature differs" if missing_t else "present"})
    for fmt in sorted(fmt_rows):
        f = fmt_rows[fmt]
        out.append({"kind": "data_format", "name": fmt, "configs": "", "template_args": "",
                    "kernels": ", ".join(sorted(f["kernels"])), "cache_rows": str(f["rows"]),
                    "quasar_status": "present" if fmt in known_formats else "missing",
                    "quasar_definitions": QUASAR_TYPES if fmt in known_formats else "",
                    "missing_template_args": "", "quasar_template_args": "", "missing_runtime_args": "",
                    "similar_quasar_apis": ""})
    for n, row in enumerate(out, 1):
        row["id"] = str(n)
    return out


def review(path: pathlib.Path, row_id: str, verdict: str, note: str) -> int:
    rows = read_csv(path)
    row = next((r for r in rows if r["id"] == row_id), None)
    if row is None:
        print(f"ERROR: no row {row_id} in {path}")
        return 2
    if row["quasar_status"] in ("present", "not an LLK"):
        print(f"ERROR: row {row_id} ({row['name']}) is {row['quasar_status']}; only candidates are reviewed")
        return 2
    if not note.strip():
        print("ERROR: --note must cite the Quasar source that decides it")
        return 2
    row["review"], row["review_note"] = verdict, note.strip()
    write_csv(path, LLK_GAPS_COLUMNS, rows)
    open_rows = sum(r["quasar_status"] in ("missing", "signature differs") and not r["review"] for r in rows)
    print(f"{row['name']}: {verdict}; {open_rows} candidates left to review")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("compute_csv", type=pathlib.Path, help="cache_compute.csv or the elfray compute export")
    p_check.add_argument("--tt-metal", type=pathlib.Path, required=True, help="a tt-metal checkout holding the commit")
    p_check.add_argument("--run-dir", type=pathlib.Path,
                         help="run to write llk_gaps_quasar.csv into; its tt_metal_commit is the ref")
    p_check.add_argument("--ref", help="tt-metal commit to read Quasar sources at (without --run-dir)")
    p_check.add_argument("--out", type=pathlib.Path, help="output CSV (without --run-dir)")
    p_review = sub.add_parser("review")
    p_review.add_argument("gaps_csv", type=pathlib.Path)
    p_review.add_argument("id")
    p_review.add_argument("verdict", choices=LLK_GAP_VERDICTS)
    p_review.add_argument("--note", required=True)
    args = parser.parse_args()
    if args.cmd == "review":
        return review(args.gaps_csv, args.id, args.verdict, args.note)
    run = None
    if args.run_dir:
        run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
        ref, out_path = run.get("tt_metal_commit", ""), args.run_dir / "llk_gaps_quasar.csv"
    else:
        ref, out_path = args.ref, args.out
    if not ref or not out_path:
        print("ERROR: give --run-dir, or --ref and --out")
        return 2
    try:
        sources = quasar_sources(args.tt_metal, ref)
        known = quasar_formats(args.tt_metal, ref)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2
    apis, fmt_rows = collect(read_csv(args.compute_csv))
    if not apis:
        print(f"ERROR: no API rows in {args.compute_csv}")
        return 2
    rows = check(apis, fmt_rows, sources, known)
    earlier = {(r["kind"], r["name"]): r for r in read_csv(out_path)} if out_path.exists() else {}
    finding = ("quasar_status", "missing_template_args", "quasar_definitions")
    for row in rows:
        before = earlier.get((row["kind"], row["name"]), {})
        # A verdict holds only for the finding it was made on; new Quasar code needs a new reading.
        keep = row["quasar_status"] in ("missing", "signature differs") and all(
            before.get(f) == row[f] for f in finding)
        row["review"] = before.get("review", "") if keep else ""
        row["review_note"] = before.get("review_note", "") if keep else ""
    write_csv(out_path, LLK_GAPS_COLUMNS, rows)
    counts = collections.Counter((r["kind"], r["quasar_status"]) for r in rows)
    summary = {f"{kind} {status}": n for (kind, status), n in sorted(counts.items())}
    if run is not None:
        run["llk_gaps"] = {"arch": "quasar", "ref": ref, "compute_csv": str(args.compute_csv.resolve()),
                           "date": datetime.date.today().isoformat(), "counts": summary}
        (args.run_dir / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    to_review = sum(r["quasar_status"] in ("missing", "signature differs") and not r["review"] for r in rows)
    print(f"{len(apis)} LLK APIs and {len(fmt_rows)} data formats checked against Quasar at {ref[:12]}: "
          + ", ".join(f"{k} {v}" for k, v in summary.items()) + f"; {to_review} candidates to review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
