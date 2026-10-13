"""Prompt cache lifetime of each coordinator turn when coordinator.cache_ttl is "auto".

A 1h cache write costs 2x input and a 5m one 1.25x; a read costs the same either way, and a turn
sent at 5m still reads a prefix an earlier turn wrote at 1h. So a turn that follows a 1h turn with
the same prefix (system prompt and stable context, action schema, model, effort, agent CLI) soon
enough to find its entries alive goes at 5m. Any other turn goes at 1h, so the many gaps of 5-60 min
between turns keep hitting. A 5m turn never extends the window: only a 1h turn's start counts.

The 1h turns are kept per prefix in the kv table, so the choice survives a daemon restart, and a
turn whose prefix alternates (a raised effort, say) finds its own 1h entry again."""
from __future__ import annotations

import hashlib
import json
import os
import time

AUTO = "auto"
KEY = "coord_cache_1h"   # kv: prefix key -> {"ts": start of its last 1h turn, the parts it was keyed on}
WARM_MIN = 50            # coordinator.cache_warm_min: a 1h turn younger than this is warm
KEEP = 8                 # prefixes remembered
PARTS = ("prompt", "schema", "model", "effort", "cli")


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def cli_stamp(binary: str | None) -> str:
    """Fingerprint of the agent CLI: where its binary resolves to, its mtime and size. An update
    (a new version swapped in or relinked) changes it; "" when there is no binary."""
    if not binary:
        return ""
    try:
        real = os.path.realpath(binary)
        st = os.stat(real)
    except OSError:
        return ""
    return _h(f"{real}:{st.st_mtime_ns}:{st.st_size}")


def fingerprint(system: str | None, context: str | None, schema: dict | None, model: str, effort: str,
                cli: str) -> dict:
    return {"prompt": _h(f"{system or ''}\0{context or ''}"),
            "schema": _h(json.dumps(schema, sort_keys=True)) if schema is not None else "",
            "model": str(model or ""), "effort": str(effort or ""), "cli": str(cli or "")}


def _key(fp: dict) -> str:
    return _h(json.dumps({k: fp.get(k, "") for k in PARTS}, sort_keys=True))


def _ts(entry: dict) -> float:
    ts = entry.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else 0.0


def _entries(state) -> dict:
    return {k: v for k, v in state.items() if isinstance(v, dict)} if isinstance(state, dict) else {}


def choose(state, fp: dict, now: float, warm_min: float = WARM_MIN) -> tuple[str, str]:
    """(ttl, reason) for a turn with prefix `fp`, given the kv state of earlier 1h turns."""
    entries = _entries(state)
    try:
        warm_s = float(warm_min) * 60
    except (TypeError, ValueError):
        warm_s = WARM_MIN * 60
    hit = entries.get(_key(fp))
    if hit:
        age = now - _ts(hit)
        if 0 <= age < warm_s:
            return "5m", f"warm: same prefix sent at 1h {age / 60:.0f} min ago"
        return "1h", f"cold: same prefix last sent at 1h {max(age, 0) / 60:.0f} min ago"
    if not entries:
        return "1h", "cold: no earlier 1h turn"
    last = max(entries.values(), key=_ts)
    changed = [k for k in PARTS if last.get(k, "") != fp.get(k, "")]
    return "1h", "changed since the last 1h turn: " + (", ".join(changed) or "prefix")


def record(state, fp: dict, now: float | None = None) -> dict:
    """The kv state with a 1h turn of prefix `fp` started at `now`; keeps the newest KEEP prefixes."""
    entries = _entries(state)
    entries[_key(fp)] = {"ts": time.time() if now is None else now, **{k: fp.get(k, "") for k in PARTS}}
    newest = sorted(entries.items(), key=lambda kv: _ts(kv[1]), reverse=True)[:KEEP]
    return dict(newest)


# Agents whose prompt cache follows the session (Provider.cache_key_per_session) miss it on every
# fresh session, however stable the prefix. Their coordinator turns continue the last turn's
# session instead, while that is cheaper than starting over: same prefix and agent, ended under
# coordinator.thread_warm_min ago, under coordinator.thread_max_turns turns (under 2 turns: off),
# history under THREAD_GROWTH x its first turn's prompt, and its last continued turn read the cache.
THREAD_WARM_MIN = 10
THREAD_MAX_TURNS = 8
THREAD_GROWTH = 2.0


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pick_thread(last: dict | None, fp: dict, provider: str, now: float, cfg: dict,
                saved=lambda session: True) -> tuple[str, dict]:
    """(session to continue or "", the turn's thread note) for a coordinator turn with prefix `fp`
    (fingerprint()), given `last`, the newest finished coordinator run. `saved(session)` says the
    agent can still continue that session."""
    key = _key(fp)
    fresh = lambda why: ("", {"key": key, "turn": 1, "why": why})
    max_turns = _num(cfg.get("thread_max_turns"), THREAD_MAX_TURNS)
    if max_turns < 2:
        return fresh("off")
    if not last:
        return fresh("no earlier turn")
    try:
        prev = (json.loads(last.get("note") or "{}").get("turn") or {}).get("thread") or {}
    except (ValueError, AttributeError):
        prev = {}
    if last.get("provider") != provider or not last.get("session_id"):
        return fresh("the last turn left no session of this agent")
    if last.get("status") != "ok":
        return fresh(f"the last turn ended {last.get('status')}")
    if not isinstance(prev, dict) or prev.get("key") != key:
        return fresh("prefix changed")
    age = now - _num(last.get("ended") or last.get("started"), 0.0)
    if not 0 <= age <= _num(cfg.get("thread_warm_min"), THREAD_WARM_MIN) * 60:
        return fresh(f"cold: the last turn ended {max(age, 0) / 60:.0f} min ago")
    turn = int(_num(prev.get("turn"), 1))
    if turn >= max_turns:
        return fresh(f"thread full: {turn} turns")
    read = int(last.get("cache_read_tokens") or 0)
    sent = read + int(last.get("input_tokens") or 0) + int(last.get("cache_write_tokens") or 0)
    base = int(_num(prev.get("base"), 0)) or sent
    if sent > THREAD_GROWTH * base:
        return fresh(f"thread grew to {sent} prompt tokens from {base}")
    if turn >= 2 and read < sent / 2:
        return fresh("its last continued turn missed the cache")
    if not saved(last["session_id"]):
        return fresh("the session is no longer saved")
    return last["session_id"], {"key": key, "turn": turn + 1, "base": base, "why": "continued"}
