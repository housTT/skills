"""List the local runs of one model under <root>/op-analysis/<model>, with identity and state."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import measurements  # noqa: E402
from validate_static import validate  # noqa: E402


def impl_slug(implementation: str) -> str:
    """Name of the implementation directory: a module file stands for its directory, and a `tt` directory
    for its parent, so `.../ttnn_resnet/tt/model.py`, `.../rtdetr/tt` and `.../resnet50/quasar` give
    `ttnn_resnet`, `rtdetr` and `quasar`."""
    directory = pathlib.PurePosixPath(implementation)
    if directory.suffix == ".py":
        directory = directory.parent
    return directory.parent.name if directory.name == "tt" else directory.name


def inventory(root: pathlib.Path, model: str) -> list[dict]:
    out = []
    for run_json in sorted((root / "op-analysis" / model).glob("**/run.json")):
        run_dir = run_json.parent
        try:
            out.append(_entry(run_dir, json.loads(run_json.read_text(encoding="utf-8"))))
        except Exception as exc:  # one broken run must not hide the others
            out.append({"path": str(run_dir), "format": "unreadable", "error": f"{type(exc).__name__}: {exc}",
                        "implementation": "", "source": "", "commit": "", "date": "", "destinations": [],
                        "static": "", "measurements": [], "drive_url": ""})
    return out


def _entry(run_dir: pathlib.Path, run: dict) -> dict:
    entry = {"path": str(run_dir), "implementation": "", "source": run.get("source", ""),
             "commit": run.get("tt_metal_commit", "")[:7], "date": run.get("date", ""),
             "destinations": run.get("destinations") if isinstance(run.get("destinations"), list) else [],
             "static": "", "measurements": [], "drive_url": run.get("drive_url") or ""}
    # Runs from plugin 0.3.0 and older (a `targets` list or a single `destination`) are listed
    # but cannot be extended.
    if "source" not in run or "destination" in run:
        entry["format"] = "old"
    else:
        entry["format"] = "current"
        entry["implementation"] = impl_slug(run.get("implementation", ""))
        if run.get("kind") != "static" and isinstance(run.get("destinations", []), list):
            # Written at setup; a preflight run may already have done source measurement items.
            entry["static"] = "not started"
            entry["measurements"] = measurements.items(run_dir)
            return entry
        errors, _, _ = validate(run_dir)
        entry["static"] = "done" if not errors else f"{len(errors)} validator errors"
        # Measurement items are derived from valid tables only.
        entry["measurements"] = [] if errors else measurements.items(run_dir)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("root", type=pathlib.Path)
    p_list.add_argument("model")
    p_list.add_argument("--json", action="store_true")
    args = parser.parse_args()
    out = inventory(args.root, args.model)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not out:
        print(f"no runs under {args.root / 'op-analysis' / args.model}")
    for n, r in enumerate(out, 1):
        if r["format"] == "unreadable":
            print(f"{n}. {r['path']} (unreadable: {r['error']})")
            continue
        if r["format"] != "current":
            print(f"{n}. {r['path']} (old format; cannot be extended)")
            continue
        pending = [m["item"] for m in r["measurements"] if m["state"] != "done"]
        print(f"{n}. {r['implementation']} on {r['source']} @ {r['commit']} ({r['date']}): static {r['static']}; "
              f"destinations {', '.join(r['destinations']) or 'none'}; "
              f"measurements pending: {', '.join(pending) or 'none'}; "
              f"{'uploaded: ' + r['drive_url'] if r['drive_url'] else 'not uploaded'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
