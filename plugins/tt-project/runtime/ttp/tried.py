# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The tried-work ledger: what this project already tried, shelved or landed, so the coordinator
never re-queues it by mistake (task_add checks it; the digest shows the entries that matter now).

It is derived model-free on each read from the task history, which is never pruned, and the
hand-offs stored with it: finished tasks (reviews excluded, cancelled ones that never ran too) from
the last LEDGER_DAYS, at most LEDGER_ROWS of them. Each entry has the task id, title, a normalised
subject key, an outcome (OUTCOMES), the date in the home zone and a one-line result. A hand-off may
name its outcome (`outcome`) and list each separate thing it tried (`tried`: [{title, outcome,
result}]); each of those is an entry of its own, so one task that shelved one lever and kept
another records both.

Matching (`similar`) is wider than the near-duplicate check on open tasks: titles are compared by
stemmed content words, and numbers in the title and spec (bounds, sizes, rates) count as the work's
parameters, so a reworded title with the same bounds still matches, while one whose numbers differ
is other work."""
from __future__ import annotations

import re
import time
from datetime import datetime

from . import timefmt
from .db import load_result

LEDGER_DAYS = 14           # history task_add checks against
LEDGER_ROWS = 400          # finished tasks read at most (newest first)
DIGEST_ROWS = 6            # ledger lines a digest shows
OUTCOMES = ("landed", "kept", "shelved", "rejected", "failed", "cancelled", "done")
SETTLED = ("landed", "kept", "shelved", "rejected")   # a result was reached: repeating it the same day is waste
RESULT_CHARS = 100
TITLE_CHARS = 80

_STOP = set("a an the of to for and or in on at by with from into vs via is be as it its this that".split())
# Words that say how work is queued, not what it is.
_GENERIC = set("code work task try retry again rerun redo new next continue follow up followup re requeue "
               "queue another more".split())
# Summary wording, checked in this order when the hand-off names no outcome.
_SAID = (("shelved", re.compile(r"\b(shelv\w*|parked|abandon\w*|not worth\w*|no (?:gain|win|improvement)|"
                                r"reverted|backed out)\b", re.I)),
         ("rejected", re.compile(r"\b(rejected|ruled out)\b", re.I)),
         ("kept", re.compile(r"\b(kept|keeping)\b", re.I)),
         ("landed", re.compile(r"\b(landed|merged|pushed)\b", re.I)))
_DATES = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?Z?)?|\b\d{1,2}:\d{2}(?::\d{2})?\b|#\d+")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:e[-+]?\d+)?)", re.I)
_DIFFERS = re.compile(r"\b(differ\w*|unlike|instead of)\b", re.I)


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def words(text: str) -> set[str]:
    """Stemmed content words, numbers left out (they are parameters, see params)."""
    return {_stem(w) for w in re.findall(r"[a-z][a-z0-9_]*", (text or "").lower())
            if w not in _STOP and w not in _GENERIC and len(w) > 1}


def subject_key(title: str) -> str:
    """The title's normalised subject: its stemmed content words, sorted."""
    return " ".join(sorted(words(title)))[:120]


def params(text: str) -> set[str]:
    """The numbers a title or spec names (bounds, sizes, rates), normalised (1.50 -> 1.5); dates,
    times and task references are not parameters."""
    out = set()
    for n in _NUMBER.findall(_DATES.sub(" ", text or "")):
        try:
            out.add(f"{float(n):g}")
        except ValueError:
            pass
    return out


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def similar(title1: str, spec1: str, title2: str, spec2: str) -> bool:
    """Whether new work (title1, spec1) repeats earlier work (title2, spec2)."""
    p1, p2 = params(f"{title1} {spec1}"), params(f"{title2} {spec2}")
    shared = len(p1 & p2)
    share = shared / len(p1) if p1 else 0.0
    n1, n2 = params(title1), params(title2)
    if n1 and n2 and n1 != n2 and share < 0.5:
        return False   # titles naming other numbers (candidates, iterations, bounds) are other work
    tw1, tw2 = words(title1), words(title2)
    if tw1 and tw1 == tw2:
        return True
    tj, to = _jaccard(tw1, tw2), _overlap(tw1, tw2)
    sw1, sw2 = words(spec1), words(spec2)
    sj = _jaccard(sw1, sw2) if sw1 and sw2 and len(sw1 | sw2) >= 4 else tj
    if (tj >= 0.7 and sj >= 0.45) or (tj >= 0.4 and sj >= 0.7):
        return True
    if shared and share >= 0.5 and (to >= 0.5 or sj >= 0.35):
        return True    # the same parameters on the same subject: a reworded repeat
    return sj >= 0.6 and to >= 0.25


def _outcome(task: dict, result: dict, landed: bool) -> str:
    said = str(result.get("outcome") or "").strip().lower()
    if said in OUTCOMES:
        return said
    if task["status"] in ("failed", "cancelled"):
        return task["status"]
    text = str(result.get("summary") or "")
    for name, rx in _SAID:
        if name == "kept" and landed:
            break
        if rx.search(text):
            return name
    return "landed" if landed else "done"


def _line(text) -> str:
    """The first sentence of `text`, on one line, clipped."""
    one = " ".join(str(text or "").split())
    one = re.split(r"(?<=[.;!?])\s", one, maxsplit=1)[0]
    return one if len(one) <= RESULT_CHARS else one[: RESULT_CHARS - 1] + "…"


def entries(db, where=None, now: float | None = None, days: float = LEDGER_DAYS) -> list[dict]:
    """The ledger, newest first: {id, title, spec, key, outcome, ts, date, result}."""
    now = time.time() if now is None else now
    rows = db.q("SELECT id, title, spec, kind, status, attempts, spent_usd, result, "
                "COALESCE(updated, created, 0) ts FROM tasks WHERE status IN ('done','failed','cancelled') "
                "AND kind != 'review' AND COALESCE(updated, created, 0) >= ? ORDER BY ts DESC, id DESC LIMIT ?",
                (now - days * 86400, LEDGER_ROWS))
    landed = {r["task"] for r in db.q("SELECT DISTINCT task FROM task_landings")} if rows else set()
    out = []
    for t in rows:
        if t["status"] == "cancelled" and not t["attempts"] and not t["spent_usd"]:
            continue   # never ran: nothing was tried
        res = load_result(t["result"])
        base = {"id": t["id"], "ts": float(t["ts"]), "date": timefmt.long(float(t["ts"]), where)}
        items = [i for i in (res.get("tried") or []) if isinstance(i, dict) and str(i.get("title") or "").strip()] \
            if isinstance(res.get("tried"), list) else []
        for i in items:
            said = str(i.get("outcome") or "").strip().lower()
            title = str(i["title"]).strip()
            out.append({**base, "title": title, "spec": f"{i.get('spec') or ''} {i.get('result') or ''}",
                        "key": subject_key(title), "outcome": said if said in OUTCOMES else "done",
                        "result": _line(i.get("result"))})
        out.append({**base, "title": t["title"] or "", "spec": t["spec"] or "", "key": subject_key(t["title"] or ""),
                    "outcome": _outcome(t, res, t["id"] in landed),
                    "result": _line(res.get("result") or res.get("summary") or t.get("spec"))})
    return out


def same_day(entry: dict, where=None, now: float | None = None) -> bool:
    tz = timefmt.tzinfo(where)
    return datetime.fromtimestamp(entry["ts"], tz).date() == \
        datetime.fromtimestamp(time.time() if now is None else now, tz).date()


def match(db, title: str, spec: str, where=None, skip=None, now: float | None = None) -> dict | None:
    """The ledger entry new work repeats: today's settled results first, then the newest. `skip` is
    the task the new one continues."""
    hits = [e for e in entries(db, where, now) if str(e["id"]) != str(skip)
            and similar(title, spec or "", e["title"], e["spec"])]
    if not hits:
        return None
    today = [e for e in hits if e["outcome"] in SETTLED and same_day(e, where, now)]
    return (today or hits)[0]


def differs(spec: str, entry: dict) -> bool:
    """Whether a forced task's spec says how it differs from the entry it matched: it names the
    entry's task (#id) and says how it differs."""
    return bool(re.search(rf"#{entry['id']}\b", spec or "")) and bool(_DIFFERS.search(spec or ""))


def describe(entry: dict, where=None, now: float | None = None) -> str:
    """'task #12 'title' was shelved today at 12:00 PDT (result: ...)'."""
    when = (f"today at {timefmt.short(entry['ts'], where, now if now is not None else entry['ts'])}"
            if same_day(entry, where, now) else f"on {entry['date']}")
    title = entry["title"] if len(entry["title"]) <= TITLE_CHARS else entry["title"][: TITLE_CHARS - 1] + "…"
    verb = {"done": "finished", "failed": "failed", "cancelled": "cancelled after a run"}.get(entry["outcome"],
                                                                                           entry["outcome"])
    return f"task #{entry['id']} '{title}' was {verb} {when}" + (f" (result: {entry['result']})" if entry["result"] else "")


def rejection(entry: dict, where=None, now: float | None = None) -> str:
    return (f"already tried: {describe(entry, where, now)}. Do not queue it again; if this is really new work, "
            f"add 'differs from #{entry['id']}: <how>' to the spec and resend with force: true")


def digest_lines(db, where=None, now: float | None = None, text: str = "") -> list[str]:
    """A few ledger lines for the digest: today's settled results, then entries this turn's events and
    messages (`text`) name by subject. Empty when there are none."""
    now = time.time() if now is None else now
    got = entries(db, where, now)
    said = words(text)
    named = [e for e in got if e["key"] and len(set(e["key"].split()) & said) >= min(2, len(e["key"].split()))]
    today = [e for e in got if e["outcome"] in SETTLED and same_day(e, where, now)]
    pick, seen = [], set()
    for e in today[: DIGEST_ROWS // 2] + named + today:
        k = (e["id"], e["title"])
        if k not in seen:
            seen.add(k)
            pick.append(e)
    if not pick:
        return []
    lines = [f"## Tried work (ledger, last {LEDGER_DAYS} d; task_add rejects a repeat of any entry): "
             f"today's results and ones this turn names"]
    for e in pick[:DIGEST_ROWS]:
        when = timefmt.short(e["ts"], where, now)
        title = e["title"] if len(e["title"]) <= TITLE_CHARS else e["title"][: TITLE_CHARS - 1] + "…"
        lines.append(f"- #{e['id']} {e['outcome']} {when}: {title}" + (f" — {e['result']}" if e["result"] else ""))
    return lines
