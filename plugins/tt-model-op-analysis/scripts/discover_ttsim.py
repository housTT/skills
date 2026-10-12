#!/usr/bin/env python3
"""Discover local TTsim checkouts and per-architecture build readiness."""

import argparse
import json
import os
import subprocess
from pathlib import Path


ARCHITECTURES = {
    "wormhole_b0": "wh",
    "blackhole": "bh",
    "quasar": "qsr",
    "trizor": "tzr",
}


def _git_sha(path):
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _candidate_paths(explicit, tt_metal_home):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("TTSIM_HOME"):
        candidates.append(Path(os.environ["TTSIM_HOME"]))
    if tt_metal_home:
        parent = Path(tt_metal_home).resolve().parent
        candidates.extend((parent / "ttsim-private", parent / "ttsim"))

    unique = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _recommended_run(arch, build_dir, library, tt_metal_home):
    if not tt_metal_home:
        return None

    tt_metal = Path(tt_metal_home).expanduser().resolve()
    python = tt_metal / "python_env" / "bin" / "python"
    environment = {
        "TT_METAL_HOME": str(tt_metal),
        "TT_METAL_SIMULATOR": str(library),
        "TT_METAL_SIMULATOR_HOME": str(build_dir),
        "TT_METAL_SLOW_DISPATCH_MODE": "1",
        "TT_METAL_DISABLE_SFPLOADMACRO": "1",
        "ARCH_NAME": arch,
        "CHIP_ARCH": arch,
    }
    if arch == "quasar":
        environment["TT_METAL_QUASAR_NOC_API_VERSION"] = "1"
    return {
        "working_directory": str(tt_metal),
        "python": str(python) if python.is_file() else None,
        "environment": environment,
    }


def _inspect_checkout(path, tt_metal_home):
    if not (path / "make.py").is_file() or not (path / "src").is_dir():
        return None

    architectures = []
    for arch, suffix in ARCHITECTURES.items():
        build_dir = path / "src" / "_out" / f"release_{suffix}"
        library = build_dir / "libttsim.so"
        descriptor = build_dir / "soc_descriptor.yaml"
        architecture = {
                "arch": arch,
                "build_dir": str(build_dir),
                "library": str(library),
                "library_exists": library.is_file(),
                "soc_descriptor": str(descriptor),
                "soc_descriptor_exists": descriptor.is_file(),
                "ready": library.is_file() and descriptor.is_file(),
                "build_target": f"src/_out/release_{suffix}/libttsim.so",
            }
        recommended_run = _recommended_run(arch, build_dir, library, tt_metal_home)
        if recommended_run is not None:
            architecture["recommended_run"] = recommended_run
        architectures.append(architecture)

    return {
        "path": str(path),
        "commit": _git_sha(path),
        "architectures": architectures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tt-metal-home", default=os.environ.get("TT_METAL_HOME"))
    parser.add_argument("--ttsim-home")
    args = parser.parse_args()

    checkouts = []
    for path in _candidate_paths(args.ttsim_home, args.tt_metal_home):
        checkout = _inspect_checkout(path, args.tt_metal_home)
        if checkout is not None:
            checkouts.append(checkout)

    print(json.dumps({"checkouts": checkouts}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
