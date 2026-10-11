# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bound coordinator wall time after its provider and reasoning effort are resolved."""
from __future__ import annotations

import math

HARD_CEILING_S = 3600
TURN_TIMEOUTS_S = {
    "claude": {"low": 300, "medium": 600, "high": 900, "xhigh": 1200, "max": 1500, "ultra": 1500},
    "codex": {"low": 300, "medium": 600, "high": 1200, "xhigh": 1800, "max": 2400, "ultra": 2400},
    "cursor": {"low": 300, "medium": 600, "high": 1200, "xhigh": 1800, "max": 2400, "ultra": 2400},
}


def positive(value, default: float) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else default
    except (ValueError, TypeError):
        return default


def turn_timeout(cfg: dict, provider: str, effort: str, prompt_bytes: int) -> float:
    c = cfg.get("coordinator") or {}
    defaults = TURN_TIMEOUTS_S.get(provider, {})
    table = c.get("turn_timeouts_s") or {}
    row = table.get(provider, {}) if isinstance(table, dict) else {}
    base = positive(row.get(effort) if isinstance(row, dict) else None, defaults.get(effort, 600))
    # Old projects often explicitly stored 600: it remains a floor, never suppresses an effort raise.
    base = max(base, positive(c.get("turn_timeout_s"), 0))
    # Input size is only a modest allowance; reasoning effort is the main driver.
    extra = math.ceil(max(0, prompt_bytes - 32000) / 8000) * 60
    ceiling = min(HARD_CEILING_S, positive(c.get("turn_timeout_ceiling_s"), HARD_CEILING_S))
    return min(ceiling, base + extra)
