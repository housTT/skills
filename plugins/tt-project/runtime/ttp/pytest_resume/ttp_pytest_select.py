# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin the push queue loads (`-p ttp_pytest_select`) to run only a check's prompt size cap
tests (a cap probe, pushpass.select_env).

TTP_PYTEST_SELECT is a regex (delivery.push_cap_tests): this run keeps only the tests whose name, the
part of the node id after its last `::`, it matches (re.search, as conflictfix.cap_failure reads a
failing id) and deselects the rest. A run that keeps none exits 5, pytest's "no tests" status."""
from __future__ import annotations

import os
import re

ENV = "TTP_PYTEST_SELECT"


def pytest_collection_modifyitems(config, items):
    pattern = os.environ.get(ENV)
    if not pattern:
        return
    rx = re.compile(pattern)
    gone = [i for i in items if not rx.search(i.nodeid.rpartition("::")[2])]
    if gone:
        items[:] = [i for i in items if rx.search(i.nodeid.rpartition("::")[2])]
        config.hook.pytest_deselected(items=gone)
