# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free warning for a command watcher that opens many issues on one subject.

A watcher whose lines carry changing text (a varying error reason, a duration such as 'for 0.7h', a
long line that splits at '; ') opens a new issue for each variant or fragment. When one watcher source
opens more than `screen.same_subject_warn` issues (default 3; 0 turns it off) with the same subject
within 24 h, the daemon logs one line and records it for the coordinator's digest and the daily
review. It never wakes anyone and changes no issue: the fix is in the watcher's output (a stable JSON
`key`, or `"whole": true`)."""
from __future__ import annotations

import time

from .db import DB

WINDOW_S = 86400.0
DEFAULT = 3
KEY = "watcher_same_subject"   # kv: "<source>\n<subject>" -> {source, subject, n, at}
FIX = 'give its JSON lines a stable "key" (or "whole": true), see the tt-project-harness skill'


def threshold(cfg: dict) -> int:
    try:
        return int((cfg.get("screen") or {}).get("same_subject_warn", DEFAULT))
    except (TypeError, ValueError):
        return DEFAULT


def counts(db: DB, source: str, now: float, window_s: float = WINDOW_S) -> list[tuple[str, int]]:
    """(subject, issues opened) for `source` over the last `window_s`; no subject is ''."""
    return [(r["subject"], int(r["n"])) for r in db.q(
        "SELECT COALESCE(subject,'') AS subject, COUNT(*) AS n FROM issues WHERE source=? AND first_seen>? "
        "GROUP BY COALESCE(subject,'')", (source, now - window_s))]


def check(db: DB, cfg: dict, sources: list[str], now: float | None = None) -> list[str]:
    """Record each source and subject over the threshold; return a log line for each one not
    warned about in the last 24 h. A known one only has its count updated."""
    limit = threshold(cfg)
    if limit <= 0:
        return []
    now = time.time() if now is None else now
    warned = {k: w for k, w in (db.kv(KEY, {}) or {}).items() if now - float(w.get("at") or 0) < WINDOW_S}
    new, changed = [], False
    for source in sources:
        for subject, n in counts(db, source, now):
            if n <= limit:
                continue
            k = f"{source}\n{subject}"
            if k in warned:
                if warned[k]["n"] != n:
                    warned[k]["n"], changed = n, True
                continue
            warned[k] = {"source": source, "subject": subject, "n": n, "at": now}
            new.append(describe(warned[k]))
    if new or changed or len(warned) != len(db.kv(KEY, {}) or {}):
        db.set_kv(KEY, warned)
    return new


def describe(w: dict) -> str:
    subject = f"subject '{w['subject']}'" if w["subject"] else "no subject"
    return f"{w['source']} opened {w['n']} issues on {subject} within 24 h: {FIX}"


def recent(db: DB, since: float, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return sorted((w for w in (db.kv(KEY, {}) or {}).values()
                   if since < float(w.get("at") or 0) and now - float(w.get("at") or 0) < WINDOW_S),
                  key=lambda w: (w["source"], w["subject"]))


def digest_lines(db: DB, since: float, now: float | None = None) -> list[str]:
    """Warnings raised since the coordinator's last turn: nothing to wake for, a watcher to fix."""
    found = recent(db, since, now)
    if not found:
        return []
    return ["## Noisy command watchers (no wake; fix the watcher's output)"] + [f"- {describe(w)}" for w in found]


def review_lines(db: DB, now: float | None = None) -> list[str]:
    """The daily review's lines: every warning of the last 24 h."""
    now = time.time() if now is None else now
    return [describe(w) for w in recent(db, now - WINDOW_S, now)]
