# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider credits and free window resets: what lets an account go on past a full plan window.

A provider's rate-limit answer may say more than its window percentages:
- paid credits (`credits: {hasCredits, unlimited, balance}`): the account keeps working past the
  window, billed per use. The window then does not hold new work, but that spend counts toward the
  project's dollar caps and the global cap (budget.evaluate). `spendControlReached` holds it again;
- free window resets (`rateLimitResetCredits: {availableCount, credits: [{id, expiresAt, ...}]}`):
  one consumed resets the windows at once.

`parse` reads them from any provider's answer into one structure; a missing or malformed field is
None (unknown), never "has credits". The daemon stores the latest reading per provider (`record`).

Auto-consume (budget.reset_credits_auto, default on) spends one free reset, earliest-expiring
first, at most once per window of a provider:
- held: a window is at the line (100 - reserve_pct) and resets more than
  budget.reset_credit_min_hours (12) from now. A window that resets sooner just waits;
- expiring: the earliest credit expires within budget.reset_credit_expiry_hours (48) and a window
  is at least budget.reset_credit_expiry_use_pct (50) % used, so a credit that would lapse unused
  is not wasted on an empty window.
The consume carries a fresh idempotency key, saved with the credit and window BEFORE the call, and
reused on every retry: a crash or a lost answer never consumes a second credit. A saved consume
whose credit has left the provider's list is taken as done.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from .db import DB

KV = "credits:"            # credits:<provider> -> the latest parsed reading, with "ts" and "account"
PENDING = "reset_consume:"  # reset_consume:<provider> -> the consume in progress (saved before the call)
USED = "reset_used:"       # reset_used:<provider> -> [{credit, window, against, why, outcome, ts, reported}]
FRESH_S = 30 * 60          # an older reading says nothing about now
SAME_WINDOW_S = 600        # a window's reset time jitters by seconds between readings
RETRY_S = 600              # a consume that got no clear answer is retried (same key) after this
MAX_TRIES = 3              # then the window gets no more tries
KEEP_USED = 20
DEFAULTS = {"reset_credits_auto": True, "reset_credit_min_hours": 12.0, "reset_credit_expiry_hours": 48.0,
            "reset_credit_expiry_use_pct": 50.0}


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> float | None:
    """An epoch time in seconds (or milliseconds), or an ISO 8601 time; None otherwise."""
    n = _num(v)
    if n is not None:
        return n / 1000 if n > 1e11 else n if n > 0 else None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse(answer: Any) -> dict:
    """The credit fields of a rate-limit answer, read from the answer itself or its `rateLimits`.
    Unknown fields are None; free resets are sorted earliest-expiring first (unknown expiry last)."""
    srcs = [s for s in (answer, answer.get("rateLimits") if isinstance(answer, dict) else None)
            if isinstance(s, dict)]

    def find(key: str) -> Any:
        return next((s[key] for s in srcs if s.get(key) is not None), None)

    c = find("credits")
    c = c if isinstance(c, dict) else {}
    r = find("rateLimitResetCredits")
    r = r if isinstance(r, dict) else {}
    lst = []
    for x in r.get("credits") if isinstance(r.get("credits"), list) else []:
        if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"] and x.get("status") in (None, "available"):
            lst.append({"id": x["id"], "expires_at": _ts(x.get("expiresAt"))})
    lst.sort(key=lambda x: (x["expires_at"] is None, x["expires_at"] or 0))
    count = r.get("availableCount")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else (
        len(lst) if isinstance(r.get("credits"), list) else None)
    kind = find("rateLimitReachedType")
    return {"paid": {"has_credits": _bool(c.get("hasCredits")), "unlimited": _bool(c.get("unlimited")),
                     "balance": _num(c.get("balance"))},
            "spend_control_reached": _bool(find("spendControlReached")),
            "reached_type": kind if isinstance(kind, str) else None,
            "free_resets": count, "resets": lst}


def paid_open(reading: dict | None) -> bool:
    """Whether paid credits let the account go on past its windows: credits or unlimited reported
    true, and no spend control reached."""
    if not reading:
        return False
    p = reading.get("paid") or {}
    return (p.get("has_credits") is True or p.get("unlimited") is True) and reading.get("spend_control_reached") is not True


def record(db: DB, provider: str, reading: dict, account: str = "", now: float | None = None) -> None:
    db.set_kv(KV + provider, {**reading, "ts": now or time.time(), "account": account or ""})


def latest(db: DB, provider: str, now: float | None = None) -> dict | None:
    """The provider's latest credit reading, or None when it is older than FRESH_S."""
    now = now or time.time()
    r = db.kv(KV + provider)
    return r if isinstance(r, dict) and now - float(r.get("ts") or 0) <= FRESH_S else None


def settings(cfg: dict) -> dict:
    b = cfg.get("budget") or {}
    return {k: b.get(k, v) if b.get(k) is not None else v for k, v in DEFAULTS.items()}


def used(db: DB, provider: str) -> list[dict]:
    v = db.kv(USED + provider) or []
    return v if isinstance(v, list) else []


def _spent_on(u: list[dict], w) -> bool:
    return any(x.get("window") == w.window and w.resets_at and abs(float(x.get("against") or 0) - float(w.resets_at))
               < SAME_WINDOW_S for x in u)


def decide(db: DB, cfg: dict, provider: str, windows: list, reading: dict | None, now: float | None = None) -> dict | None:
    """The free reset to consume now, or None: {credit, expires_at, window, against, why}."""
    now = now or time.time()
    s = settings(cfg)
    if not s["reset_credits_auto"] or not reading:
        return None
    creds = reading.get("resets") or []
    if not creds:
        return None
    line = 100.0 - float((cfg.get("budget") or {}).get("reserve_pct", 10))
    mine = sorted((w for w in windows if w.provider == provider and w.resets_at),
                  key=lambda w: -float(w.resets_at))
    done = used(db, provider)
    first = creds[0]

    def pick(w, why: str) -> dict:
        return {"credit": first["id"], "expires_at": first.get("expires_at"), "window": w.window,
                "against": float(w.resets_at), "utilization": round(w.utilization, 1), "why": why}
    for w in mine:
        if w.utilization >= line and w.resets_at - now > float(s["reset_credit_min_hours"]) * 3600 \
                and not _spent_on(done, w):
            return pick(w, "held")
    if first.get("expires_at") and first["expires_at"] - now <= float(s["reset_credit_expiry_hours"]) * 3600:
        for w in mine:
            if w.utilization >= float(s["reset_credit_expiry_use_pct"]) and not _spent_on(done, w):
                return pick(w, "expiring")
    return None


def _finish(db: DB, provider: str, pick: dict, outcome: str, now: float) -> dict:
    entry = {k: pick.get(k) for k in ("credit", "expires_at", "window", "against", "utilization", "why")}
    entry.update({"outcome": outcome, "ts": now, "reported": False})
    with db.tx():
        db.set_kv(USED + provider, (used(db, provider) + [entry])[-KEEP_USED:])
        db.x("DELETE FROM kv WHERE key=?", (PENDING + provider,))
    return entry


def consume(db: DB, cfg: dict, provider: str, prov: Any, windows: list, reading: dict | None,
            now: float | None = None) -> dict | None:
    """Consume one free reset when `decide` says so, crash-safe. Returns the finished entry
    ({..., "outcome"}), {"error": ...} while a try failed and will be retried, or None."""
    now = now or time.time()
    pending = db.kv(PENDING + provider)
    if pending and reading is not None and pending.get("credit") not in {c["id"] for c in reading.get("resets") or []}:
        # Its credit left the list: the call went through before an answer was lost (or the
        # user consumed it). Either way that window had its reset.
        return _finish(db, provider, pending, "reset (confirmed by the next reading)", now)
    want = decide(db, cfg, provider, windows, reading, now)
    if pending:
        if not want:   # the credit is still there and nothing holds any more: the call never took effect
            db.x("DELETE FROM kv WHERE key=?", (PENDING + provider,))
            return None
        if now - float(pending.get("last_try") or 0) < RETRY_S:
            return None
        pick = pending
    elif want:
        pick = {**want, "key": uuid.uuid4().hex, "ts": now, "attempts": 0}
    else:
        return None
    pick = {**pick, "attempts": int(pick.get("attempts") or 0) + 1, "last_try": now}
    db.set_kv(PENDING + provider, pick)   # saved before the call: a retry reuses the same key
    try:
        res = prov.consume_reset(pick["credit"], pick["key"])
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    if res is None:   # the provider cannot consume resets
        db.x("DELETE FROM kv WHERE key=?", (PENDING + provider,))
        return None
    if res.get("ok"):
        return _finish(db, provider, pick, str(res.get("outcome") or "reset"), now)
    err = str(res.get("error") or res.get("outcome") or "no clear answer")[:300]
    if pick["attempts"] >= MAX_TRIES:
        return _finish(db, provider, pick, f"failed: {err}", now)
    db.set_kv(PENDING + provider, {**pick, "error": err})
    return {"error": err, "attempts": pick["attempts"]}


def unreported(db: DB, provider: str) -> list[dict]:
    return [u for u in used(db, provider) if not u.get("reported")]


def mark_reported(db: DB, provider: str, done: list[dict]) -> None:
    """Mark the entries `done` reported (by credit and time: one added meanwhile stays unreported)."""
    keys = {(u.get("credit"), u.get("ts")) for u in done}
    with db.tx():
        db.set_kv(USED + provider, [{**u, "reported": True} if (u.get("credit"), u.get("ts")) in keys else u
                                    for u in used(db, provider)])


def report_text(provider: str, u: dict, fmt=None) -> str:
    fmt = fmt or (lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)))
    why = ("its window was full and would not reset for hours" if u.get("why") == "held"
           else "the credit was about to expire unused")
    if str(u.get("outcome") or "").startswith("failed"):
        return (f"Could not use a free {provider} window reset ({why}): {u['outcome']}. "
                f"Held work waits for the window's own reset.")
    exp = f", expiring {fmt(u['expires_at'])}" if u.get("expires_at") else ""
    return (f"Used one free {provider} window reset{exp}: the {u.get('window')} window was at "
            f"{float(u.get('utilization') or 0):.0f}% ({why}). Outcome: {u.get('outcome')}. Held work starts again.")


def summary(reading: dict | None) -> str:
    """'2 free resets', 'paid credits', both, or '' for the budget line."""
    if not reading:
        return ""
    parts = []
    n = reading.get("free_resets")
    if n:
        parts.append(f"{n} free reset{'s' if n != 1 else ''}")
    if reading.get("spend_control_reached") is True:
        parts.append("spend limit reached")
    elif paid_open(reading):
        parts.append("paid credits")
    return ", ".join(parts)


def unblock_path(db: DB, cfg: dict, provider: str, now: float | None = None) -> str:
    """For the blocker sweep: what can lift a provider's window hold, or ''."""
    now = now or time.time()
    r = latest(db, provider, now)
    bits = []
    if r and r.get("free_resets"):
        auto = "consumed automatically when held" if settings(cfg)["reset_credits_auto"] else "auto-consume off"
        bits.append(f"{r['free_resets']} free window reset(s) ({auto})")
    if paid_open(r):
        bits.append("paid credits (the window does not hold; the dollar caps apply)")
    last = (used(db, provider) or [None])[-1]
    if last and now - float(last.get("ts") or 0) < 7 * 86400:
        bits.append(f"last free reset {(now - float(last['ts'])) / 3600:.1f} h ago: {last.get('outcome')}")
    return "; ".join(bits)
