"""Show the four source-analysis methods for a run: which are done, which can run here, and why not.

The static trace is the run's base: op_table.csv is built from it, so a new run always does it
first. The other three are measurement items the user picks, several at once:

- `source:tracy` needs a device of the run's source profile and a tt-metal build with Tracy.
- `source:graph` needs that device and a tt-metal commit whose graph capture records the program
  factory of each launch.
- `source:cache` needs that device to build the cache and the elfray engine to read it. Without the
  engine the cache can still be built in the device run and read later; without the device, an
  engine can still read a cache built earlier at the run's commit.

--device is the `profile` field that probe_device.py printed, or `none` when no device answered.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cache_report import INSTALL_HINT, find_elfray  # noqa: E402
from opa_schema import LAUNCH_PROFILES, METHOD_ITEMS, METHOD_LABELS, SOURCE_METHODS  # noqa: E402

FACTORY_FIELD = "program_factory_type"
GRAPH_CONSTS = "ttnn/api/ttnn/graph/graph_consts.hpp"
BUILD_DIRS = ("build", "build_Release", "build_RelWithDebInfo", "build_Debug")


def tracy_build(checkout: pathlib.Path) -> str | None:
    """None when the build has Tracy on, else why it is not known to be on."""
    for name in BUILD_DIRS:
        cache = checkout / name / "CMakeCache.txt"
        if cache.is_file():
            for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ENABLE_TRACY:"):
                    return None if line.split("=", 1)[1].strip().upper() in ("ON", "1", "TRUE") else (
                        f"{name} was configured with ENABLE_TRACY=OFF; rebuild with the profiler")
            return None  # ENABLE_TRACY is on unless configured off
    return f"no CMake build directory under {checkout}"


def graph_factories(checkout: pathlib.Path, commit: str) -> str | None:
    """None when graph capture at `commit` records program factories, else why not."""
    ref = commit or "HEAD"
    out = subprocess.run(["git", "-C", str(checkout), "show", f"{ref}:{GRAPH_CONSTS}"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return f"cannot read {GRAPH_CONSTS} at {ref[:7]}"
    if FACTORY_FIELD not in out.stdout:
        return f"graph capture at {ref[:7]} does not record program factories; use a newer commit"
    return None


def methods(run: dict, checkout: pathlib.Path | None, device: str, elfray: str | None) -> list[dict]:
    source = run.get("source", "")
    marks = run.get("measurements", {})
    if device == "none":
        no_device = "no device answered the probe"
    elif device != source:
        no_device = f"the device is {device}, the run's source is {source}"
    else:
        no_device = None
    out = []
    for method in SOURCE_METHODS:
        row = {"method": method, "label": METHOD_LABELS[method], "item": "", "state": "", "reason": ""}
        if method == "static":
            row["state"] = "done" if run.get("kind") == "static" else "required"
            row["reason"] = "" if row["state"] == "done" else "op_table.csv is built from it; it runs first"
            out.append(row)
            continue
        item, key = METHOD_ITEMS[method]
        row["item"] = item
        if run.get(key):
            row["state"] = "done"
            out.append(row)
            continue
        reasons = []
        if source not in LAUNCH_PROFILES:
            reasons.append(f"source {source} has no device run (TTsim covers a Quasar source)")
        elif no_device:
            reasons.append(no_device)
        if method == "tracy" and checkout:
            reasons += [r for r in [tracy_build(checkout)] if r]
        if method == "graph" and checkout:
            reasons += [r for r in [graph_factories(checkout, run.get("tt_metal_commit", ""))] if r]
        if method == "cache" and not elfray and not reasons:
            row["state"] = "available, read later"
            reasons = [f"the device run can build the cache; reading it needs the elfray engine ({INSTALL_HINT})"]
        elif method == "cache" and elfray and reasons:
            row["state"] = "read an existing cache"
            reasons = [f"no new cache here ({'; '.join(reasons)}); a cache this run's commit built can be read"]
        else:
            row["state"] = "unavailable" if reasons else "available"
        row["reason"] = "; ".join(reasons)
        mark = marks.get(item, {})
        if mark.get("state") == "failed":
            row["reason"] = "; ".join(x for x in (row["reason"], f"failed earlier: {mark.get('note', '')}") if x)
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("--device", required=True, help="profile from probe_device.py, or none")
    parser.add_argument("--tt-metal", type=pathlib.Path, help="the tt-metal checkout the device runs from")
    parser.add_argument("--elfray", help="path to the elfray engine; searched for when omitted")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    rows = methods(run, args.tt_metal, args.device, find_elfray(args.elfray))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for n, row in enumerate(rows, 1):
        item = f" [{row['item']}]" if row["item"] else ""
        reason = f": {row['reason']}" if row["reason"] else ""
        print(f"{n}. {row['label']}{item}: {row['state']}{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
