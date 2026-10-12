"""Refuse measured work when the tt-metal checkout is not the commit the static run analysed."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def mismatch(run_dir: pathlib.Path, tt_metal: pathlib.Path) -> str | None:
    """Return why the checkout cannot be used for this run, or None when it matches."""
    want = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["tt_metal_commit"]
    head = _git(tt_metal, "rev-parse", "HEAD")
    if head != want:
        return (f"checkout is at {head[:7]}, the static run analysed {want[:7]}; "
                f"check out {want} and rebuild, or start a new static run")
    modified = _git(tt_metal, "status", "--porcelain", "--untracked-files=no")
    if modified:
        return f"checkout at {head[:7]} has modified tracked files:\n{modified}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("--tt-metal", type=pathlib.Path, required=True)
    args = parser.parse_args()
    problem = mismatch(args.run_dir, args.tt_metal)
    if problem:
        print(f"ERROR: {problem}")
        return 1
    print("OK: checkout matches the static run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
