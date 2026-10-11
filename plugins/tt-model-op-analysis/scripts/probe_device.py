#!/usr/bin/env python3
"""Print the architecture and physical grid of one accessible TT device as JSON."""

import argparse
import json

import ttnn


def _grid(grid):
    return {"x": int(grid.x), "y": int(grid.y)}


def _profile(arch, dram_grid):
    arch_lower = str(arch).lower()
    if "quasar" in arch_lower:
        return "quasar"
    if "blackhole" in arch_lower:
        if dram_grid and dram_grid["x"] == 8:
            return "p150"
        if dram_grid and dram_grid["x"] == 7:
            return "p100"
    return "custom"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", type=int, default=0)
    args = parser.parse_args()

    device = None
    try:
        device = ttnn.open_device(device_id=args.device_id)
        arch = str(ttnn.get_arch_name())
        compute_grid = _grid(device.compute_with_storage_grid_size())
        dram_grid = None if "quasar" in arch.lower() else _grid(device.dram_grid_size())
        print(
            json.dumps(
                {
                    "device_id": args.device_id,
                    "arch": arch,
                    "compute_grid": compute_grid,
                    "dram_grid": dram_grid,
                    "profile": _profile(arch, dram_grid),
                },
                sort_keys=True,
            )
        )
    finally:
        if device is not None:
            ttnn.close_device(device)


if __name__ == "__main__":
    main()
