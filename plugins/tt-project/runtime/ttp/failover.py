# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider failover: move tasks off a provider that cannot start new work for a long while.

A queued (deferred included) or waiting task whose provider (its own pin, else the core provider)
cannot start new work moves, model-free, to another provider that can, when that provider's stop
lasts more than coordinator.provider_failover_after_h hours (default AFTER_H; 0 turns failover off).
A provider cannot start new work when its auth breaker is open (logged out: no known end), its
limit pause runs, or its budget gate is red (a plan window at the line, a dollar cap, the runaway
guard). Its end is the pause's end, or for a red gate the reset of every window or cap holding it;
a red gate whose end is unknown moves nothing.

Targets: coordinator.provider_failover_to (provider names, in order of preference) and then the
core provider. A target must be configured and installed, logged in, not paused, with a gate that
is not red and allows new work. The move clears the task's model and effort pin (one provider's
names), maps the pinned effort to the tier that ran it (e.g. Codex xhigh to deep), and adds a spec
note saying what moved, why and when the old provider resets. Running tasks are never touched: each
is reported once. A task (or one it continues or reviews) labelled `pin:required` stays put.
Every move is an event in the feed (kind KIND), listed in the daily review, and one low alert.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import timefmt
from .db import DB

KIND = "provider_failover"
REQUIRED_LABEL = "pin:required"
NOTE_PREFIX = "[provider failover"
AFTER_H = 4.0
REPORTED_KEY = "failover_reported"     # kv: {"<task>:<provider>": ts} running tasks already reported
CHAIN_RE = re.compile(r"^(?:continues|auto_review|review_fix):(\d+)$")
EFFORT_RANK = ("minimal", "low", "medium", "high", "xhigh", "max")
TIERS = ("light", "standard", "deep")


def after_h(cfg: dict) -> float:
    try:
        v = (cfg.get("coordinator") or {}).get("provider_failover_after_h", AFTER_H)
        return max(0.0, float(AFTER_H if v is None else v))
    except (TypeError, ValueError):
        return AFTER_H


def targets(cfg: dict) -> list[str]:
    """Providers a task may move to, best first: coordinator.provider_failover_to, then the core one."""
    raw = (cfg.get("coordinator") or {}).get("provider_failover_to") or []
    names = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
    core = cfg.get("core_provider") or "claude"
    out = [n for n in (str(n).strip() for n in names) if n in (cfg.get("providers") or {})] + [core]
    return [n for i, n in enumerate(out) if n not in out[:i]]


def stop(gate: Any, pause: dict | None, logged_out: bool, now: float) -> tuple[str, float | None] | None:
    """Why the provider cannot start new work and when that ends (None: logged out, no known end), or
    None if it can or its red gate has no known end. `gate` is a budget Gate (or its dict), `pause`
    its limited:<provider> record."""
    if logged_out:
        return "logged out", None
    if pause and float(pause.get("until") or 0) > now:
        return f"provider limit: {pause.get('note') or 'paused'}", float(pause["until"])
    g = gate.as_dict() if hasattr(gate, "as_dict") else (gate or {})
    if not g or g.get("allow_new_work", True) or g.get("level") != "red":
        return None
    reasons, n = list(g.get("reasons") or []), g.get("numbers") or {}
    ends: list[float | None] = []
    for r in reasons:
        if " window at " in r and "the project stops at" in r:
            rows = [w for w in n.get("plan") or [] if w.get("allowed") == 0 and r.startswith(f"{w.get('window')} ")]
            ends += [w.get("resets_at") for w in rows] or [None]
        elif r.startswith("global daily cap"):
            ends.append(n.get("global_resets_at"))
        elif r.startswith("cap reached"):
            ends.append(n.get("day_end"))   # a rolling day or week has no fixed end
        elif r.startswith("runaway guard"):
            ends.append(now + 3600)
        elif r.startswith("provider limit"):
            ends.append(float(pause["until"]) if pause and pause.get("until") else None)
    if not ends or any(e is None for e in ends):
        return None   # no known end: nothing to weigh against provider_failover_after_h
    return "; ".join(reasons), max(float(e) for e in ends)


def _labels(task: dict) -> list:
    try:
        v = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def required(db: DB, task: dict, depth: int = 8) -> bool:
    """Whether the task's pin is marked required: its own `pin:required` label, or one on a task it
    continues, reviews or fixes (so a review or fix keeps the pin its origin requires)."""
    seen, todo = set(), [task]
    while todo and len(seen) <= depth:
        t = todo.pop()
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        for lb in _labels(t):
            if lb == REQUIRED_LABEL:
                return True
            m = CHAIN_RE.match(lb) if isinstance(lb, str) else None
            if m and (up := db.task(int(m.group(1)))):
                todo.append(up)
    return False


def mapped_tier(cfg: dict, provider: str, task: dict) -> str:
    """The tier that matches the task's effort pin on `provider` (its tier when it pins none)."""
    eff = (task.get("effort") or "").strip().lower()
    if not eff:
        return task.get("tier") or "standard"
    tiers = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers") or {}
    for name in reversed(TIERS):
        if str((tiers.get(name) or {}).get("effort") or "").lower() == eff:
            return name
    rank = EFFORT_RANK.index(eff) if eff in EFFORT_RANK else 3
    return "light" if rank <= 1 else "standard" if rank <= 3 else "deep"


def note(move: dict, where) -> str:
    """What moved, why and until when, in plain words (the spec note and feed line say this)."""
    t, old = move["task"], move["old"]
    pin = ", ".join(f"{k} {t[k]}" for k in ("model", "effort") if t.get(k))
    until = f"until {timefmt.long(move['ends'], where)}" if move["ends"] else "with no known end"
    return (f"moved from {old}" + (f" ({pin})" if pin else "") + f" to {move['new']}, tier {move['tier']}: "
            f"{old} cannot start new work ({move['why']}) {until}. Its pin was not marked required "
            f"(label {REQUIRED_LABEL}).")


NOTE_RE = re.compile(r"(?:\n\n|^)" + re.escape(NOTE_PREFIX) + r"[^\n]*")


def strip_notes(spec: str) -> str:
    """The spec as it was before its failover notes (a move is no change of the task's own spec)."""
    return NOTE_RE.sub("", spec or "") if NOTE_PREFIX in (spec or "") else spec or ""


def plan(db: DB, cfg: dict, stops: dict[str, tuple[str, float | None]], ok: list[str],
         now: float) -> tuple[list[dict], list[dict]]:
    """The moves to make now and the running tasks to report: ([{task, old, new, tier, why, ends}],
    [{task, provider, why, ends}]). `stops` maps each provider that cannot start new work to (why, end);
    `ok` lists the targets that can, best first."""
    limit = after_h(cfg)
    if not limit or not stops:
        return [], []
    core = cfg.get("core_provider") or "claude"
    moves, running = [], []
    for t in db.q("SELECT * FROM tasks WHERE status IN ('queued','waiting','running') ORDER BY priority, id"):
        old = t["provider"] or core
        if old not in stops:
            continue
        why, ends = stops[old]
        if ends is not None and ends - now <= limit * 3600:
            continue
        if t["status"] == "running":
            running.append({"task": t, "provider": old, "why": why, "ends": ends})
            continue
        new = next((p for p in ok if p != old), None)
        if not new or required(db, t):
            continue
        moves.append({"task": t, "old": old, "new": new, "tier": mapped_tier(cfg, old, t), "why": why, "ends": ends})
    return moves, running


def apply(db: DB, cfg: dict, move: dict, now: float, where) -> str:
    """Re-route one task as `move` says; returns the feed line."""
    t, body = move["task"], " ".join(note(move, where).split())
    text = f"{NOTE_PREFIX} {timefmt.long(now, where)}] {body[0].upper()}{body[1:]}"
    line = f"#{t['id']} {t['title'][:80]}: {body}"
    with db.tx():
        db.update_task(t["id"], provider=move["new"], model=None, effort=None, tier=move["tier"],
                       spec=f"{t['spec']}\n\n{text}" if t["spec"] else text)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", KIND, "low", line[:500], "handled", t["id"]))
    return line


def report_running(db: DB, items: list[dict], now: float, where) -> list[str]:
    """Feed lines for running tasks on a stopped provider, each once per task and provider."""
    seen = db.kv(REPORTED_KEY, {}) or {}
    out = []
    for r in items:
        key = f"{r['task']['id']}:{r['provider']}"
        if key in seen:
            continue
        until = f"until {timefmt.long(r['ends'], where)}" if r["ends"] else "with no known end"
        line = (f"#{r['task']['id']} {r['task']['title'][:80]}: left running on {r['provider']}, which cannot "
                f"start new work ({r['why']}) {until}; a later run of it moves unless its pin is required")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", KIND, "low", line[:500], "handled", r["task"]["id"]))
        seen[key] = now
        out.append(line)
    if out:
        db.set_kv(REPORTED_KEY, {k: v for k, v in seen.items() if now - float(v) < 14 * 86400})
    return out


def review_lines(db: DB, now: float | None = None, hours: float = 24) -> list[str]:
    """For the daily review: every failover event of the last `hours`."""
    now = time.time() if now is None else now
    rows = db.q("SELECT text FROM events WHERE kind=? AND ts>=? ORDER BY ts", (KIND, now - hours * 3600))
    return [r["text"] for r in rows]
