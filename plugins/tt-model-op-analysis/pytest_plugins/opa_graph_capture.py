"""pytest plugin: write a ttnn graph capture of each test to the file named by $OPA_GRAPH_CAPTURE.

Load it with `pytest -p opa_graph_capture` and this directory on PYTHONPATH. It does nothing when
the variable is unset.

tt-metal's own graph-report fixture imports the capture into a SQLite database and deletes the JSON;
graph_report.py needs the JSON, which records each launch's device op and program factory.
Capture starts after the test's device fixture opens and ends before it closes, as in tt-metal's
fixture, so device setup and teardown are not captured. Other function-scoped setup (weight upload,
preprocessing on device) is; graph_report.py flags it as counts that do not divide by the inferences.
"""

from __future__ import annotations

import os
import pathlib

import pytest

ENV = "OPA_GRAPH_CAPTURE"
_written: list[pathlib.Path] = []


def _target() -> pathlib.Path:
    path = pathlib.Path(os.environ[ENV])
    # One file per test; the entry test is normally a single node, so the first keeps the name.
    if not _written:
        return path
    return path.with_name(f"{path.stem}_{len(_written) + 1}{path.suffix}")


@pytest.fixture(autouse=True)
def opa_graph_capture(request):
    if not os.environ.get(ENV):
        yield
        return
    import ttnn

    if ttnn.graph.is_graph_capture_active():
        yield
        return
    # tt-metal's device fixtures are `device` and `*mesh_device` (mesh_device, bh_1d_mesh_device, ...).
    for name in request.fixturenames:
        if name == "device" or name.endswith("mesh_device"):
            request.getfixturevalue(name)
    ttnn.graph.begin_graph_capture(ttnn.graph.RunMode.NORMAL)
    try:
        yield
    finally:
        if ttnn.graph.is_graph_capture_active():
            path = _target()
            path.parent.mkdir(parents=True, exist_ok=True)
            ttnn.graph.end_graph_capture_to_file(str(path))
            _written.append(path)
            print(f"\n[opa_graph_capture] wrote {path}")
