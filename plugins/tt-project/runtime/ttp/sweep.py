# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Blocker sweep: a regular high-effort re-check of everything that is not moving.

Every coordinator.blocker_sweep_h hours (default 6; 0 off), and sooner on a signal, the daemon queues
one SWEEP_KIND event while anything is stuck. That event makes the coordinator's turn a high-effort
one (coordinator.EFFORT_EVENT_TRIGGERS) and adds a 'Blocker sweep' section to its digest: every
blocked, waiting, in-review, setting-held and deferred task and every paused resource, with its age,
its reason and its last re-check. The coordinator records exactly one outcome per item with the
`sweep` action (OUTCOMES). `still_blocked` needs a re-checkable condition (`until_probe`, or an
`until` that is not only "keep it blocked") and a next check (`start_after`); `human_indispensable`
needs a `why`. An item the next sweep finds without an outcome since the last one, or whose
`still_blocked` had no re-checkable condition or passed its next check, or that an ask has held past
coordinator.ask_rethink_h (askrethink.py), is carried and flagged. A
valid `still_blocked` whose next check is still ahead, on an item that has not changed since (status,
spec, reason) and is no loop, is carried forward as one collapsed CARRIED line that needs no action; a
sweep due only to its interval is skipped while every item is carried. A hold no turn can lift (`ends`:
a user pause or a time-only deferral, userpause) is carried without an outcome, listed as HELD BY USER
PAUSE or DEFERRED (no action), and left out of signals and loop detection.

Signals that bring a sweep forward (never closer than coordinator.blocker_sweep_min_gap_h to the last
one): a paused resource is no longer paused, an ask was resolved, a stuck task's dependency finished,
a provider's limit or logout pause ended, a provider's account or budget kind changed (budgetkind),
a `still_blocked` next check came due, or another module
called signal() (the hook for later checks, such as a broker's health lifting a pause).

Loops: a task handed off stuck twice in a row with the same normalized reason, or whose spec changed
while it stayed in the same stuck state since the last sweep, is shown as LOOP: resolve it, do not
re-addend it. Reviews: a task in review longer than coordinator.review_backlog_h (default 6) gets a
line on its review task: none queued, queued but held (and why), running, failed or finished.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

from . import askrethink, credits, failover, holdloops, obligations, settinghold, timefmt, unblock, userpause
from .db import DB, deferral, dependency_ids, load_result
from .coordlimits import positive

SWEEP_KIND = "blocker_sweep"
STATE_KEY = "blocker_sweep"            # kv: {"last", "n", "items": {key: snapshot}, "flags": {key: why}, "paused": [...],
#                                         "seen": {key: {"status", "since"}}: when each item was first seen stuck}
OUTCOMES_KEY = "blocker_sweep_outcomes"  # kv: {key: {"outcome", "ts", "until", "probe", "next", "why", "sweep"}}
SIGNAL_KEY = "blocker_sweep_signals"   # kv: [{"ts", "why"}]: signal() requests not yet swept
OUTCOMES = ("unblocked", "rerouted", "retired", "closed", "still_blocked", "human_indispensable")
EVERY_H = 6.0
MIN_GAP_H = 1.0
REVIEW_BACKLOG_H = 6.0
REVIEW_LONG_H = 6.0     # the daily review lists every item stuck longer than this
LOOP = "loop: resolve, do not re-addend"
REASON_CHARS = 200
# An `until` that only restates the hold names no condition anyone could re-check.
SELF_REF = re.compile(r"\b(retain|keep|keeps|remain|remains|stay|stays|leave|still)\s+(it\s+|status\s+)?"
                      r"(blocked|in review|waiting|paused|held)\b|\baddend(um|a)?\b|\bno change\b|\bas before\b|"
                      r"^\s*(blocked|unchanged|n/?a|none|tbd|-)\s*\.?\s*$", re.I)
UNTIL_MIN_CHARS = 12
STUCK_EVENTS = tuple(unblock.STUCK)


def _cfg(cfg: dict, key: str, default: float) -> float:
    try:
        v = (cfg.get("coordinator") or {}).get(key, default)
        return max(0.0, float(default if v is None else v))
    except (TypeError, ValueError):
        return default


def _dur(s: float | None) -> str:
    return "unknown" if s is None else f"{max(0.0, s) / 3600:.1f}h"


def _clip(text: Any, n: int = REASON_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def signal(db: DB, why: str) -> None:
    """Ask for an early sweep (kept until the next sweep starts). The hook for other modules: call it
    when something a stuck item may wait on changed, e.g. a resource's health check lifts its pause."""
    sigs = db.kv(SIGNAL_KEY, []) or []
    sigs = [s for s in sigs if isinstance(s, dict)][-19:]
    db.set_kv(SIGNAL_KEY, sigs + [{"ts": time.time(), "why": _clip(why, 200)}])


def _last_stuck_events(db: DB, tid: int, n: int = 2) -> list[dict]:
    return db.q(f"SELECT ts, kind, text, data FROM events WHERE task=? AND kind IN ({','.join('?' * len(STUCK_EVENTS))}) "
                f"ORDER BY ts DESC, id DESC LIMIT ?", (tid, *STUCK_EVENTS, n))


def _reason(t: dict) -> str:
    return _clip(t["blocked_reason"] or load_result(t["result"]).get("summary") or "")


def _spec_sha(t: dict) -> str:
    # A provider failover's note is no change of the task's own spec (no LOOP flag for it).
    return hashlib.sha256(failover.strip_notes(t["spec"] or "").encode()).hexdigest()[:16]


def _moved_at(db: DB, tid: int) -> float:
    """When the task last moved on (a requeue, a hand-off out of stuck, or a run started): 0 if never."""
    ev = db.one(f"SELECT MAX(ts) ts FROM events WHERE task=? AND kind IN ({','.join('?' * len(unblock.MOVED))})",
                (tid, *sorted(unblock.MOVED)))
    run = db.one("SELECT MAX(started) ts FROM runs WHERE task=? AND role!='coordinator'", (tid,))
    return max(float((ev or {}).get("ts") or 0), float((run or {}).get("ts") or 0))


def items(db: DB, now: float | None = None, where=None) -> list[dict]:
    """Everything stuck now: {key, kind, task (or None), resource (or None), title, age_s (None when
    unknown), since, reason, status, spec_sha, ends}. Kinds: blocked, setting-held, waiting (not a self-wait),
    review, deferred, paused, user-pause (a task whose only hold is a user-pause probe). `ends` is "user"
    or "time" for a hold no turn can lift (a user pause or user-set resource pause, a time-only deferral), else None. A task's age comes from its open stuck episode (unblock._task_episodes);
    without that history (a block the coordinator set) from when the sweep first saw it in this status
    (remember()), first seeded from its last update, so a later update (an addendum) never resets it.
    Times in reasons are in the home zone of `where` (a Project, its settings or a zone name; None is UTC)."""
    now = time.time() if now is None else now
    out: list[dict] = []
    since_review = db.review_since()
    seen = (db.kv(STATE_KEY, {}) or {}).get("seen") or {}
    cfg = userpause.settings(where)
    for t in db.q("SELECT * FROM tasks WHERE status IN ('blocked','waiting','review','queued') ORDER BY id"):
        kind, start, reason, ends = t["status"], None, _reason(t), None
        if kind == "queued":
            d = deferral(t)
            if not d or (not d.get("when") and float(t["not_before"] or 0) <= now):
                continue
            kind, start = "deferred", d.get("since")
            reason = _clip(d.get("why") or d.get("when") or "") or (
                f"starts after {timefmt.long(float(t['not_before']), where)}"
                if t["not_before"] else "")
            if userpause.time_only(t, now):
                ends = "time"
        else:
            last = _last_stuck_events(db, t["id"], 1)
            if kind == "waiting" and last and last[0]["kind"] == "task_waiting" and unblock.is_self_wait(last[0]["data"]):
                continue
            if kind == "review":
                start = since_review.get(t["id"])
            else:
                eps = unblock._task_episodes(db, t, now)
                if eps and eps[-1]["open"]:
                    start = eps[-1]["start"]
            if kind == "waiting" and last:
                reason = _clip(unblock.wait_reason(last[0])) or reason
            if kind == "blocked" and settinghold.held_key(t):
                kind = "setting-held"
        if kind != "review" and (pause := userpause.probe_of(t, cfg)):
            kind, ends, reason = "user-pause", "user", f"held by user pause until `{_clip(pause, 160)}` passes"
        if not isinstance(start, (int, float)):
            prev = seen.get(f"task:{t['id']}") or {}
            start = prev.get("since") if prev.get("status") == t["status"] else None
            if not isinstance(start, (int, float)) or start < _moved_at(db, t["id"]):
                start = float(t["updated"]) if t["updated"] else None
        out.append({"key": f"task:{t['id']}", "kind": kind, "task": t["id"], "resource": None,
                    "title": t["title"], "age_s": None if start is None else max(0.0, now - float(start)),
                    "since": start, "reason": reason, "status": t["status"], "spec_sha": _spec_sha(t),
                    "ends": ends})
    for name, v in sorted(db.paused_resources().items()):
        since = v.get("since") if isinstance(v.get("since"), (int, float)) else None
        out.append({"key": f"resource:{name}", "kind": "paused", "task": None, "resource": name, "title": name,
                    "age_s": None if since is None else max(0.0, now - float(since)), "since": since,
                    "reason": _clip(f"by {v.get('by') or 'user'}" + (f": {v['reason']}" if v.get("reason") else "")),
                    "status": "paused", "spec_sha": "",
                    # A pause the user set is lifted on their word only; no turn needs to re-check it.
                    "ends": "user" if v.get("by") == "user" else None})
    for o in obligations.open_promises(db):
        pr, late = o["promise"], now >= o["promise"]["due"]
        out.append({"key": f"promise:{o['id']}", "kind": "promise", "task": None, "resource": None,
                    "promise": o["id"], "title": o["text"], "age_s": max(0.0, now - float(o["created"])),
                    "since": o["created"], "status": "overdue" if late else "promised", "spec_sha": "",
                    "reason": f"promised in chat {pr['chat']} (message #{pr['message']}), due "
                              f"{timefmt.long(pr['due'], where)}" + (" OVERDUE" if late else "")
                              + (f", owner #{', #'.join(map(str, pr['tasks']))}" if pr["tasks"] else ""),
                    # Not due yet: listed, no outcome needed; overdue: an item like any other.
                    "ends": None if late else "due"})
    return out


def _seen(state: dict, live: list[dict]) -> dict:
    """The `seen` map for these live items: an entry kept while the item stays in the same status and
    items() still ages it from that entry (a requeue or run since makes items() reject it: replaced)."""
    old = state.get("seen") or {}
    out = {}
    for i in live:
        o = old.get(i["key"]) or {}
        out[i["key"]] = o if o.get("status") == i["status"] and o.get("since") is not None \
            and o.get("since") == i["since"] else {"status": i["status"], "since": i["since"]}
    return out


def remember(db: DB, live: list[dict]) -> None:
    """Keep when each stuck item was first seen in its status (the age source for holds without history)."""
    state = db.kv(STATE_KEY, {}) or {}
    seen = _seen(state, live)
    if seen != (state.get("seen") or {}):
        db.set_kv(STATE_KEY, {**state, "seen": seen})


def loop(db: DB, item: dict, prev: dict | None) -> str | None:
    """Why this item looks like a re-block loop, or None: the same normalized stuck reason handed off
    twice in a row, or its spec changed since the last sweep while it stayed in the same stuck state."""
    if item["task"] is None or item["kind"] == "deferred" or item.get("ends"):
        return None
    last = _last_stuck_events(db, item["task"], 2)
    if len(last) == 2 and last[0]["kind"] == last[1]["kind"]:
        a, b = unblock.wait_reason(last[0]), unblock.wait_reason(last[1])
        if a and a == b:
            return f"{LOOP} (handed off {unblock.STUCK[last[0]['kind']]} twice with the same reason)"
    if prev and prev.get("status") == item["status"] and prev.get("spec_sha") and \
            prev["spec_sha"] != item["spec_sha"]:
        return f"{LOOP} (spec changed while it stayed {item['status']})"
    return None


def review_diagnosis(db: DB, tid: int, now: float, hold_of: Callable[[dict], str | None] | None = None) -> str:
    """One line on the review of a task in review: none queued, queued but held (and why), running,
    failed or finished (and so waiting on its verdict being acted on)."""
    task = db.task(tid)
    revs = [r for r in db.q("SELECT * FROM tasks WHERE kind='review' AND id>? ORDER BY id DESC", (tid,))
            if tid in dependency_ids(r) or f"auto_review:{tid}" in (r["labels"] or "")]
    if not revs:
        why = "the daemon queues one only for a code task with a diff and review.auto on" \
            if task and task["kind"] != "code" else "check review.auto, the review task cap and the branch's diff"
        return f"no review queued ({why})"
    r = revs[0]
    runs = db.q("SELECT status, started, ended FROM runs WHERE task=? ORDER BY id", (r["id"],))
    started = f", started {len(runs)}x" if runs else ""
    if r["status"] == "queued":
        held = None
        if hold_of:
            try:
                held = hold_of(r)
            except Exception as e:   # a diagnosis must never break the sweep
                held = f"unknown ({type(e).__name__})"
        unmet = [d for d in dependency_ids(r) if d != tid and (db.task(d) or {}).get("status") != "done"]
        why = (f"dispatch holds it: {held}" if held else None) or (f"waits on #{', #'.join(map(str, unmet))}" if unmet else
                       "not started yet; no dispatch hold found")
        return f"review #{r['id']} queued {_dur(now - float(r['created'] or now))}{started}, not dispatched: {why}"
    if r["status"] == "running":
        return f"review #{r['id']} running{started}"
    summary = _clip(load_result(r["result"]).get("summary") or r["blocked_reason"] or "", 160)
    if r["status"] in ("failed", "cancelled", "blocked", "waiting"):
        return f"review #{r['id']} {r['status']}{started}" + (f": {summary}" if summary else "")
    return f"review #{r['id']} {r['status']}{started}; its verdict is not acted on yet" + (
        f": {summary}" if summary else "")


def _signals(db: DB, state: dict, live: list[dict], now: float) -> list[str]:
    """What changed since the last sweep that may unblock something."""
    last = float(state.get("last") or 0)
    out = [str(s.get("why") or "signal") for s in (db.kv(SIGNAL_KEY, []) or []) if isinstance(s, dict)]
    paused_now = {i["resource"] for i in live if i["kind"] == "paused"}
    gone = sorted(set(state.get("paused") or []) - paused_now)
    if gone:
        out.append("pause ended: " + ", ".join(gone))
    for aid, r in (db.kv(unblock.RESOLVED_KEY, {}) or {}).items():
        if isinstance(r, dict) and float(r.get("ts") or 0) > last:
            out.append(f"ask #{aid} resolved")
            break
    deps = {d for i in live if i["task"] and not i.get("ends") for d in dependency_ids(db.task(i["task"]) or {"depends_on": "[]"})
            if isinstance(d, int)}
    if deps:
        done = db.q(f"SELECT DISTINCT task FROM events WHERE kind='task_done' AND ts>? AND task IN "
                    f"({','.join('?' * len(deps))})", (last, *sorted(deps)))
        if done:
            out.append("dependency done: " + ", ".join(f"#{r['task']}" for r in done))
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE 'limited:%'"):
        try:
            until = float(json.loads(r["value"]).get("until") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
        if last < until <= now:
            out.append(f"provider pause over: {r['key'].split(':', 1)[1]}")
    keys = {i["key"] for i in live if not i.get("ends")}
    for key, o in (db.kv(OUTCOMES_KEY, {}) or {}).items():
        if key in keys and o.get("outcome") == "still_blocked" and o.get("next") and last < float(o["next"]) <= now:
            out.append(f"next check due: {key}")
    return out


def due(db: DB, cfg: dict, now: float | None = None) -> str | None:
    """Why a sweep should start now, or None: nothing stuck, sweeps off, one queued already, or
    neither its interval nor a signal (past the minimum gap) calls for one."""
    now = time.time() if now is None else now
    every = _cfg(cfg, "blocker_sweep_h", EVERY_H) * 3600
    if every <= 0:
        return None
    live = items(db, now, cfg)
    remember(db, live)
    if not live or db.one("SELECT id FROM events WHERE kind=? AND status='queued' LIMIT 1", (SWEEP_KIND,)):
        return None
    state = db.kv(STATE_KEY, {}) or {}
    since = now - float(state.get("last") or 0)
    if since >= every:
        outs = db.kv(OUTCOMES_KEY, {}) or {}
        prev = state.get("items") or {}
        rethought = rethink(db, cfg, live, now)
        if not holdloops.flagged(db) and all(_carries(i, state, outs, now) and not loop(db, i, prev.get(i["key"]))
                                             and i["key"] not in rethought for i in live):
            # Every item keeps a valid, not-yet-due outcome: only a pending signal calls for a sweep.
            sigs = _signals(db, state, live, now)
            return "; ".join(sigs)[:300] if sigs else None
        return f"every {every / 3600:g}h"
    if since < min(every, _cfg(cfg, "blocker_sweep_min_gap_h", MIN_GAP_H) * 3600):
        return None
    sigs = _signals(db, state, live, now)
    return "; ".join(sigs)[:300] if sigs else None


def _snap(i: dict) -> dict:
    """What a carried outcome must still match: the item's status, spec and reason."""
    return {"status": i["status"], "spec_sha": i["spec_sha"], "reason": i["reason"]}


def carried(o: dict | None, i: dict, now: float) -> bool:
    """A still_blocked outcome that still holds for item `i`: re-checkable, its next check ahead, and
    recorded against the item as it is now. Such an item needs no new outcome at this sweep. Nor does
    a hold that ends without a turn (`ends`): a user pause or a time-only deferral."""
    if i.get("ends"):
        return True
    return bool(o and o.get("outcome") == "still_blocked" and recheckable(o) and o.get("next")
                and float(o["next"]) > now and o.get("item") == _snap(i))


def flags(db: DB, state: dict, live: list[dict], now: float) -> dict[str, str]:
    """Items the last sweep left unresolved: no outcome since it, or a `still_blocked` with no
    re-checkable condition or past its next check."""
    last = float(state.get("last") or 0)
    prev = state.get("items") or {}
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    out = {}
    for i in live:
        o = outs.get(i["key"])
        if carried(o, i, now):
            continue
        if i["key"] in prev and (not o or float(o.get("ts") or 0) < last):
            out[i["key"]] = "no outcome at the last sweep"
        elif o and o.get("outcome") == "still_blocked":
            if not recheckable(o):
                out[i["key"]] = "still_blocked without a re-checkable condition"
            elif o.get("next") and float(o["next"]) <= now:
                out[i["key"]] = "its next check is due"
    return out


def rethink(db: DB, cfg: dict, live: list[dict], now: float) -> dict[str, str]:
    """Blocked tasks an open ask has held past coordinator.ask_rethink_h (askrethink.py): a
    `still_blocked` on one is flagged, never carried. The coordinator decides the ask itself, or
    records `human_indispensable` with why only the user can answer it."""
    held = askrethink.held_tasks(db, cfg, now)
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    return {i["key"]: f"held by open ask {held[i['task']]} past {askrethink.hours(cfg):g}h: decide the ask "
                      f"yourself, or record human_indispensable with why only the user can"
            for i in live if i["task"] in held
            and (outs.get(i["key"]) or {}).get("outcome") != "human_indispensable"}


def recheckable(o: dict) -> bool:
    """A still_blocked outcome names something anyone can re-check: a probe, or an `until` that is
    not only 'keep it blocked'."""
    if str(o.get("probe") or "").strip():
        return True
    until = str(o.get("until") or "").strip()
    return len(until) >= UNTIL_MIN_CHARS and not SELF_REF.search(until)


def start(db: DB, cfg: dict, why: str, now: float | None = None,
          hold_of: Callable[[dict], str | None] | None = None) -> int | None:
    """Queue the sweep's event and record what it covers, with each long review's diagnosis (hold_of:
    why dispatch would not start a queued task now, the daemon's own check); returns the event id."""
    now = time.time() if now is None else now
    state = db.kv(STATE_KEY, {}) or {}
    live = items(db, now, cfg)
    if not live:
        return None
    prev = state.get("items") or {}
    flagged = flags(db, state, live, now)
    flagged.update(rethink(db, cfg, live, now))
    loops = {i["key"]: x for i in live if (x := loop(db, i, prev.get(i["key"])))}
    stuck = holdloops.flagged(db)
    for i in live:
        if i["task"] in stuck:
            loops[i["key"]] = "; ".join(filter(None, (loops.get(i["key"]), f"{LOOP} ({stuck[i['task']]})")))
    backlog = _cfg(cfg, "review_backlog_h", REVIEW_BACKLOG_H) * 3600
    reviews = {i["key"]: review_diagnosis(db, i["task"], now, hold_of) for i in live
               if i["kind"] == "review" and backlog > 0 and (i["age_s"] or 0) >= backlog}
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    carry = [i["key"] for i in live if i["key"] not in loops and i["key"] not in reviews
             and i["key"] not in flagged and carried(outs.get(i["key"]), i, now)]
    n = int(state.get("n") or 0) + 1
    with db.tx():
        db.set_kv(STATE_KEY, {"last": now, "n": n, "why": why, "flags": flagged, "loops": loops, "reviews": reviews,
                              "carried": carry, "paused": [i["resource"] for i in live if i["kind"] == "paused"],
                              "seen": _seen(state, live), "items": {i["key"]: _snap(i) for i in live}})
        db.x("DELETE FROM kv WHERE key=?", (SIGNAL_KEY,))
        eid = db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                   (now, "daemon", SWEEP_KIND, "normal",
                    f"Blocker sweep {n} ({why}): {len(live)} stuck item(s), {len(flagged)} flagged, "
                    f"{len(loops)} loop(s), {len(carry)} carried forward. Record one `sweep` outcome for each "
                    f"item not carried.",
                    json.dumps({"sweep": n, "items": [i["key"] for i in live], "snapshot": live}), "queued"))
    return eid


def _item_key(a: dict) -> str:
    if a.get("id") is not None:
        return f"task:{int(a['id'])}"
    if str(a.get("promise") or "").strip():
        return f"promise:{str(a['promise']).strip()}"
    name = str(a.get("resource") or "").strip()
    if not name:
        raise ValueError("sweep needs `id` (a task), `promise` (its obligation id) or `resource` (a paused resource)")
    return f"resource:{name}"


def record(db: DB, a: dict, next_at: float | None, now: float | None = None) -> str:
    """Record one `sweep` action's outcome (next_at: its `start_after`, already parsed). Raises
    ValueError on a bad one, which the turn reports back."""
    now = time.time() if now is None else now
    outcome = str(a.get("outcome") or "")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    key = _item_key(a)
    if key.startswith("task:") and not db.task(int(key[5:])):
        raise ValueError(f"no task #{key[5:]}")
    if key.startswith("promise:"):
        o = obligations.get(db, key[8:])
        if not o or not o.get("promise"):
            raise ValueError(f"no promise {key[8:]}")
        if outcome in ("closed", "retired"):   # kept or no longer owed: the promise is closed
            obligations.close_promise(db, key[8:], f"sweep {outcome}: " + str(a.get("why") or a.get("reason") or ""),
                                      "coordinator", now)
    o = {"outcome": outcome, "ts": now, "sweep": int((db.kv(STATE_KEY, {}) or {}).get("n") or 0)}
    if outcome == "still_blocked":
        o.update(until=_clip(a.get("until") or a.get("reason") or "", 300),
                 probe=_clip(a.get("until_probe") or "", 500))
        if not recheckable(o):
            raise ValueError(f"{key} still_blocked needs a re-checkable condition: `until_probe` (a shell probe) or "
                             f"`until` naming what must change (not 'keep it blocked')")
        if not next_at or next_at <= now:
            raise ValueError(f"{key} still_blocked needs `start_after`: when to check it next")
        o["next"] = next_at
        # The item as this sweep showed it: the outcome carries forward only while it stays so.
        snap = ((db.kv(STATE_KEY, {}) or {}).get("items") or {}).get(key)
        if snap:
            o["item"] = snap
    elif outcome == "human_indispensable":
        if not str(a.get("why") or "").strip():
            raise ValueError(f"{key} human_indispensable needs `why` only a person can do it")
    o["why"] = _clip(a.get("why") or a.get("reason") or a.get("text") or "", 300)
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    outs[key] = o
    db.set_kv(OUTCOMES_KEY, {k: v for k, v in outs.items() if now - float(v.get("ts") or 0) < 30 * 86400})
    return key


def _last_check(o: dict | None, now: float) -> str:
    if not o:
        return "never re-checked"
    s = f"last re-check {_dur(now - float(o['ts']))} ago: {o['outcome']}"
    if o["outcome"] == "still_blocked":
        s += f" until {_clip(o.get('probe') or o.get('until'), 120)}"
        if o.get("next"):
            s += f", next check in {_dur(float(o['next']) - now)}" if float(o["next"]) > now else ", next check due"
    elif o.get("why"):
        s += f" ({_clip(o['why'], 120)})"
    return s


def _header(n: int, carry: int = 0) -> str:
    return (f"## Blocker sweep (high effort): {n} item(s)" + (f", {carry} CARRIED" if carry else "")
            + ". Return exactly one `sweep` action per item not marked CARRIED, "
            "with `id` (task), `promise` (its id) or `resource`, and `outcome`: unblocked | rerouted | retired | closed (merged or "
            "closed) | still_blocked (both required: `until_probe` or `until`, the concrete condition, AND "
            "`start_after`, the next check: a delay such as 90m, 6h or 3d, or an ISO time such as "
            "2026-10-05T09:00 in the home zone) | human_indispensable (`why`). A CARRIED item keeps its last "
            "still_blocked until its next check; act on it only with news. Make the change itself with the usual "
            "actions in this turn. A FLAG or LOOP needs a real decision, not another addendum.")


def _carries(i: dict, state: dict, outs: dict, now: float) -> bool:
    return not any(i["key"] in (state.get(f) or {}) for f in ("flags", "loops", "reviews")) \
        and carried(outs.get(i["key"]), i, now)


def _who(i: dict, n: int) -> str:
    if i["task"]:
        return f"#{i['task']} {_clip(i['title'], n)}"
    if i.get("promise") or i["key"].startswith("promise:"):
        return f"promise {i['key'][8:]} {_clip(i['title'], n)}"
    return f"resource {i['resource']}"


def _line(i: dict, state: dict, outs: dict, now: float) -> str:
    who = _who(i, 70)
    if i.get("ends") and _carries(i, state, outs, now):
        what = {"user": "HELD BY USER PAUSE", "due": "PROMISED, NOT DUE YET"}.get(i["ends"], "DEFERRED")
        return f"- {who} | {what} (no action): {_clip(i['reason'], 160)}"
    if _carries(i, state, outs, now):
        return f"- {who} | CARRIED (no action): {_last_check(outs.get(i['key']), now)}"
    line = f"- {who} | {i['kind']} {_dur(i['age_s'])} | {_clip(i['reason']) or 'no reason given'} | " \
           f"{_last_check(outs.get(i['key']), now)}"
    for field, label in (("flags", "FLAG"), ("loops", "LOOP"), ("reviews", "review")):
        value = (state.get(field) or {}).get(i["key"])
        if value:
            line += f" | {label}: {_clip(value)}"
    return line


def _settled(db: DB, key: str) -> dict:
    """A covered item that is no longer stuck, rebuilt from its durable identity (an event queued
    before snapshots existed has keys only), so the turn still records how it ended."""
    kind, _, ident = key.partition(":")
    if kind == "task" and ident.isdigit():
        t = db.task(int(ident))
        status = t["status"] if t else "gone"
        return {"key": key, "kind": "resolved", "task": int(ident), "resource": None,
                "title": t["title"] if t else "(no longer exists)", "age_s": None, "since": None,
                "reason": f"no longer stuck (now {status}): record closed or unblocked", "status": status,
                "spec_sha": _spec_sha(t) if t else "", "ends": None}
    if kind == "promise":
        o = obligations.get(db, ident) or {}
        return {"key": key, "kind": "resolved", "task": None, "resource": None, "promise": ident,
                "title": o.get("text") or "(no longer exists)", "age_s": None, "since": None,
                "reason": "promise closed: record closed", "status": "closed", "spec_sha": "", "ends": None}
    return {"key": key, "kind": "resumed", "task": None, "resource": ident, "title": ident, "age_s": None,
            "since": None, "reason": "no longer paused: record closed or unblocked", "status": "resumed",
            "spec_sha": "", "ends": None}


def _covered(db: DB, data: dict, now: float, where=None) -> list[dict]:
    # Retain an identity even if an earlier batch unblocked it; the next batch can record closed.
    by_key = {i["key"]: i for i in data.get("snapshot", [])}
    by_key.update({i["key"]: i for i in items(db, now, where)})
    return [by_key.get(k) or _settled(db, k) for k in data.get("items", [])]


def prepare(db: DB, event_id: int, cfg: dict, now: float | None = None) -> None:
    """Split this event's coverage to fit a bounded sweep section, including events queued before
    an upgrade. Persist continuations atomically; retries reuse the same first batch."""
    now = time.time() if now is None else now
    event = db.one("SELECT * FROM events WHERE id=? AND kind=?", (event_id, SWEEP_KIND))
    if not event:
        return
    data = json.loads(event["data"] or "{}")
    live = _covered(db, data, now, cfg)
    state, outs = db.kv(STATE_KEY, {}) or {}, db.kv(OUTCOMES_KEY, {}) or {}
    budget = min(16000, max(2048, positive((cfg.get("coordinator") or {}).get("sweep_prompt_chars"), 12000)))
    flagged, loops = state.get("flags") or {}, state.get("loops") or {}
    live.sort(key=lambda i: (i["key"] not in flagged and i["key"] not in loops, -(i["age_s"] or 0)))
    batches, batch = [], []
    size = len(_header(len(live))) + 32  # room for age counters changing before the digest is built
    for i in live:
        cost = len(_line(i, state, outs, now)) + 1
        if batch and size + cost > budget:
            batches.append(batch)
            batch, size = [], len(_header(len(live))) + 32
        batch.append(i)
        size += cost
    batches.append(batch)
    with db.tx():
        for n, batch in enumerate(batches):
            payload = json.dumps({**data, "items": [i["key"] for i in batch], "snapshot": batch})
            text = (f"Blocker sweep {data.get('sweep', 0)}: {len(batch)} stuck item(s) in this batch. "
                    "Record one `sweep` outcome for each item not marked CARRIED.")
            if n == 0:
                db.x("UPDATE events SET data=?, text=? WHERE id=?", (payload, text, event_id))
            else:
                db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                     (event["ts"], "daemon", SWEEP_KIND, "normal", text, payload, "queued"))


def digest_lines(db: DB, event_ids: list[int], now: float | None = None, cfg: dict | None = None) -> list[str]:
    """Only this batch's sweep identities; later batches stay queued for consecutive turns. A provider
    held red by its budget gate gets a line with its unblock path (free window resets, paid credits,
    the last reset used), though it is not a sweep item: the daemon consumes resets itself."""
    if not event_ids:
        return []
    events = db.q(f"SELECT data FROM events WHERE kind=? AND id IN ({','.join('?' * len(event_ids))}) ORDER BY id",
                  (SWEEP_KIND, *event_ids))
    if not events:
        return []
    now = time.time() if now is None else now
    state, outs = db.kv(STATE_KEY, {}) or {}, db.kv(OUTCOMES_KEY, {}) or {}
    live = {i["key"]: i for e in events for i in _covered(db, json.loads(e["data"] or "{}"), now, cfg)}
    flagged, loops = state.get("flags") or {}, state.get("loops") or {}
    ordered = sorted(live.values(), key=lambda i: (i["key"] not in flagged and i["key"] not in loops,
                                                   -(i["age_s"] or 0)))
    carry = sum(1 for i in ordered if _carries(i, state, outs, now))
    out = [_header(len(live), carry), *(_line(i, state, outs, now) for i in ordered)]
    for prov, g in sorted((db.kv("gates", {}) or {}).items()):
        if isinstance(g, dict) and g.get("level") == "red":
            path = credits.unblock_path(db, cfg or {}, prov, now)
            out.append(f"- provider {prov} held (no sweep action; {_clip('; '.join(g.get('reasons') or []), 160)}) | "
                       f"unblock path: {path or 'none reported (no free resets or paid credits)'}")
    return out


def review_lines(db: DB, now: float | None = None, hours: float = REVIEW_LONG_H, where=None) -> list[str]:
    """For the daily review: every item stuck longer than `hours`, with its reason and sweep outcome
    (times in the home zone of `where`)."""
    now = time.time() if now is None else now
    outs = db.kv(OUTCOMES_KEY, {}) or {}
    state = db.kv(STATE_KEY, {}) or {}
    loops = state.get("loops") or {}
    rows = [i for i in items(db, now, where) if i["age_s"] is not None and i["age_s"] >= hours * 3600]
    if not rows:
        return [f"nothing stuck longer than {hours:g}h"]
    out = []
    for i in sorted(rows, key=lambda x: -x["age_s"]):
        who = _who(i, 60)
        o = outs.get(i["key"])
        out.append(f"{who}: {i['kind']} {_dur(i['age_s'])}: {i['reason'] or 'no reason given'}; sweep: "
                   + (f"{o['outcome']} {_dur(now - float(o['ts']))} ago" if o
                      else f"none needed (ends by {i['ends']})" if i.get("ends") else "no outcome yet")
                   + (" (LOOP)" if i["key"] in loops else ""))
    return out
