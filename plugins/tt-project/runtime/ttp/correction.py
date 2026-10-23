# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Corrections: a user or chat message that points out something the project should have noticed or
done itself. The coordinator fixes that case and adds a `harness` task for the class of failure (the
detector, rule or default that would have caught it), so the next case is not left to the user.

Model-free, no model calls. At a coordinator turn's start the daemon flags each new inbound message
that is a likely correction (`flag`): one matching a small generic phrase list (PHRASES), or any
message that arrives while something says the project is stuck (`open_stall`: a stall event or a
Course check line; a correction still without a class fix is not one, or it would flag every later
message, and neither is Resource trouble, a machine fault rather than a miss). The digest tags
the message `[likely correction: <why>]`. After the turn (`settle`), each flagged message is recorded
with its class fix: a `harness` task (or one titled `upstream: ...`) the turn added, or the turn's
`summary` naming `correction #<id>` with its reason. One with neither gets a Course check line in the
next digest, once (`digest_lines`); a later harness task clears it. The daily review's efficiency
audit lists each correction of the last 24 h and the task that fixed its class, or none
(`audit_line`)."""
from __future__ import annotations

import re
import time

from .db import DB

KEY = "corrections"   # kv: [{"msg", "ts", "why", "fix", "shown"}], newest last
KEEP_S = 7 * 86400
KEEP_N = 200
DAY = 86400
# Phrases of a message pointing out a miss. Generic and short: a false flag costs one tag and at most
# one Course check line; a missed one leaves the class gap open.
PHRASES = ("why didn't", "why did nobody", "why did no one", "why hasn't", "why haven't", "why wasn't",
           "keeps failing", "keep failing", "kept failing", "still failing", "stuck for", "you missed",
           "nobody noticed", "no one noticed", "didn't notice", "failed to notice", "failing to notice",
           "should have noticed", "should have caught", "lack of progress", "no progress")
# Event kinds of a stall detector (no progress on a task or the project). Any such event not yet
# handled makes the next user message a likely correction.
STALL_KINDS = ("no_progress", "stall")
FIX_KINDS = ("harness",)


def phrase(text: str) -> str:
    """The first PHRASES entry in `text`, or ''."""
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    return next((x for x in PHRASES if x in low), "")


def _records(db: DB) -> list[dict]:
    got = db.kv(KEY, []) or []
    return [r for r in got if isinstance(r, dict) and str(r.get("msg", "")).isdigit()]


def _pending(db: DB, now: float) -> list[dict]:
    """Recorded corrections with no class fix, not shown yet, younger than a day, with no harness task
    added since they arrived."""
    out = []
    for r in _records(db):
        if r.get("fix") or r.get("shown") or now - float(r.get("ts") or 0) > DAY:
            continue
        if _later_fix(db, float(r.get("ts") or 0)):
            continue
        out.append(r)
    return out


def _later_fix(db: DB, since: float) -> str:
    row = db.one(f"SELECT id FROM tasks WHERE created>=? AND (kind IN ({','.join('?' * len(FIX_KINDS))}) "
                 "OR lower(title) LIKE 'upstream:%') ORDER BY id LIMIT 1", (since, *FIX_KINDS))
    return f"#{row['id']}" if row else ""


def open_stall(db: DB, cfg: dict | None, now: float | None = None) -> str:
    """What says the project is stuck right now, or ''. Cheapest first."""
    now = time.time() if now is None else now
    if db.one(f"SELECT 1 FROM events WHERE status!='handled' AND kind IN ({','.join('?' * len(STALL_KINDS))})",
              STALL_KINDS):
        return "open stall"
    from . import course
    if course.digest_lines(db, cfg, now)[0]:
        return "open Course check"
    return ""


def flag(db: DB, cfg: dict | None, msgs: list[dict], now: float | None = None) -> dict[int, str]:
    """{message id: why} for the inbound messages in `msgs` that are likely corrections. Rows without
    a `text` column (the daemon's turn-sizing query) get theirs read here."""
    out, stall = {}, None
    bare = [int(m["id"]) for m in msgs if "text" not in m]
    if bare:
        full = {r["id"]: r for r in db.q(f"SELECT id, direction, text FROM messages WHERE id IN "
                                         f"({','.join('?' * len(bare))})", bare)}
        msgs = [full.get(int(m["id"]), m) if "text" not in m else m for m in msgs]
    for m in msgs:
        if m.get("direction", "in") != "in":
            continue
        hit = phrase(m.get("text") or "")
        if hit:
            out[int(m["id"])] = f"phrase '{hit}'"
            continue
        if stall is None:
            stall = open_stall(db, cfg, now)
        if stall:
            out[int(m["id"])] = stall
    return out


def tag(why: str) -> str:
    return f" [likely correction: {why}]"


def class_fix(db: DB, actions: list, summary: str, mid: int, since: float) -> str:
    """The turn's class fix for message `mid`: '#<task>' it added, 'reason' its summary gave, or ''."""
    titles = [str(a.get("title") or "") for a in actions if isinstance(a, dict) and a.get("type") == "task_add"
              and (a.get("kind") in FIX_KINDS or str(a.get("title") or "").strip().lower().startswith("upstream:"))]
    if titles:
        row = db.one(f"SELECT id FROM tasks WHERE created>=? AND title IN ({','.join('?' * len(titles))}) "
                     "ORDER BY id LIMIT 1", (since, *titles))
        if row:
            return f"#{row['id']}"
    if re.search(rf"\bcorrection\s*#?{int(mid)}\b", summary or "", re.I):
        return "reason"
    return ""


def settle(db: DB, note: dict, actions: list, summary: str, started: float, now: float | None = None) -> None:
    """After a coordinator turn: record its flagged messages with their class fix, and mark the
    correction lines its digest showed (those recorded before it started) as shown, with this turn's
    class fix if it made one."""
    now = time.time() if now is None else now
    recs = _records(db)
    by_msg = {int(r["msg"]): r for r in recs}
    for r in recs:
        if not r.get("fix") and not r.get("shown") and float(r.get("ts") or 0) < started:
            r["shown"] = True
            r["fix"] = class_fix(db, actions, summary, int(r["msg"]), started) or _later_fix(db, float(r["ts"]))
    for mid, why in (note.get("corrections") or {}).items():
        mid = int(mid)
        fix = class_fix(db, actions, summary, mid, started)
        if mid in by_msg:
            by_msg[mid]["fix"] = by_msg[mid].get("fix") or fix
            continue
        rec = {"msg": mid, "ts": now, "why": str(why)[:80], "fix": fix, "shown": False}
        recs.append(rec)
        by_msg[mid] = rec
    recs = [r for r in recs if now - float(r.get("ts") or 0) <= KEEP_S][-KEEP_N:]
    db.set_kv(KEY, recs)


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """One Course check line for the corrections the turn that handled them gave no class fix; []
    when there are none."""
    now = time.time() if now is None else now
    got = _pending(db, now)
    if not got:
        return []
    ids = ", ".join(f"#{r['msg']}" for r in got)
    one = got[0]["msg"] if len(got) == 1 else "<id>"
    return [f"- correction without a class fix: user message{'s' if len(got) > 1 else ''} {ids} pointed out a "
            "miss and got no `harness` task or `upstream:` note | add a `harness` task for the detector, rule or "
            f"default that would have caught it, or say `correction #{one}: <why not>` in summary"]


def audit_line(db: DB, since: float) -> str:
    """The self-efficiency audit's line: each correction since `since` and its class fix; '' with none."""
    got = [r for r in _records(db) if float(r.get("ts") or 0) >= since]
    if not got:
        return ""
    def fixed(r: dict) -> str:
        fix = r.get("fix") or ""
        return f"class fix {fix}" if fix.startswith("#") else "none (reason given)" if fix == "reason" else "none"
    return (f"corrections from the user or a chat: {len(got)}, {sum(str(r.get('fix') or '').startswith('#') for r in got)} "
            "with a class fix: " + "; ".join(f"#{r['msg']} ({r.get('why')}) {fixed(r)}" for r in got[:8])
            + (f"; and {len(got) - 8} more" if len(got) > 8 else ""))
