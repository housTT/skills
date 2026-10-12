# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Post-push deploy outcomes and durable, model-free takeover checks.

Only the reporting project's database is written. Other projects are read through their daemon
receipts, never changed by verification. Ordinary `ttp deployed` remains a strict read-only probe.
"""
from __future__ import annotations

import time

from . import overview, project, timefmt

CHECKS = "deploy_checks"
GRACE_S = 300
PROBE_S = 60
HISTORY = 20


def condition_key(name: str, version: str) -> str:
    return f"deploy:{name}:{version}"


def _probe(rec: dict, now: float) -> dict:
    return overview.deploy_status(rec["name"], rec["entry"], rec["target"], rec.get("commit"), now,
                                  project.hostname(), allow_newer=True)


def _owned(row: dict) -> bool:
    return bool(row.get("merge_pending") and row.get("merge_owner"))


def assess(rec: dict, row: dict, now: float, where=None) -> tuple[str, str, str]:
    """(outcome, kind, reason) of one probe: done; pending (an accepted owned merge before its deadline);
    or failed, kind "fault" (a daemon that is down outside an upgrade, or a current upgrade error: at once,
    whatever the deadline), "missed" (an accepted merge past its deadline) or "unowned" (never accepted).
    Shared by record, tick and holds, so the alert clears exactly when its condition does. Times are
    shown in the zone of `where` (the reporting project)."""
    if row["state"] == overview.OK:
        return "done", "", ""
    reason = row["reason"]
    if row.get("upgrade_error"):
        return "failed", "fault", f"{row['upgrade_error']}; {reason}"
    if not row.get("daemon_alive") and not row.get("upgrading"):
        return "failed", "fault", f"daemon {row.get('daemon_state') or 'unverified'}; {reason}"
    takeover = rec.get("takeover_at")
    if (_owned(row) and takeover) or rec.get("accepted"):
        # Accepted once, later steps of the same merge (files landed, a restart) wait for the same deadline.
        if now < rec["due"]:
            return "pending", "", reason
        at = timefmt.long(takeover, where) if takeover else "unknown"
        return "failed", "missed", f"missed takeover {at} (owner {rec.get('owner') or 'unknown'}); {reason}"
    if row.get("merge_owner") and not takeover:
        reason += "; no known takeover time"
    return "failed", "unowned", reason


def holds(db, arg: str, now: float) -> bool:
    rec = (db.kv(CHECKS) or {}).get("deploy:" + arg)
    return bool(rec and not rec.get("closed") and assess(rec, _probe(rec, now), now)[0] == "failed")


def _alert(p, key: str, rec: dict, kind: str, reason: str, now: float, alert=None) -> None:
    """At most one alert per condition: deduplicated by the stored kind, which a resolved condition
    resets (`_settle`); the history of what was sent is kept."""
    if rec.get("alerted") and rec.get("alerted_kind") == kind:
        return
    text = f"Deploy failed: {rec['name']} is not running tt-project {rec['target']}: {reason}."
    if alert:
        alert(key, text, severity="normal")
    elif not p.db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
        p.db.post("out", text, kind="alert", severity="normal", ref=key)
    rec.update(alerted=now, alerted_kind=kind)
    rec["history"] = (rec.get("history") or [])[-(HISTORY - 1):] + [{"at": now, "kind": kind, "commit": rec.get("commit")}]


def _settle(rec: dict, outcome: str, now: float) -> None:
    rec["outcome"] = outcome
    if outcome != "failed":
        rec.pop("alerted", None)
        rec.pop("alerted_kind", None)
    if outcome == "pending":
        rec["accepted"] = True
    if outcome == "done":
        rec["closed"] = now


def record(p, report: dict, now: float | None = None) -> dict:
    """Classify done/pending/failed, accepting only live owned merges with a takeover deadline.

    Pending is success for after_push. A repeat of the same release (version and commit) by the same
    owner keeps its original deadline across repeat deploys and daemon restarts; another commit or
    owner is a new merge with its own. The outcome is judged from the probe alone: an earlier alert
    only deduplicates, it never turns a healthy owned merge into a failure.
    """
    now = time.time() if now is None else now
    registry = project.load_registry().get("projects") or {}
    pending, rows = [], []
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        for row in report["projects"]:
            row = dict(row)
            key = condition_key(row["name"], report["target"])
            old = checks.get(key) or {}
            same = (old and not old.get("closed") and old.get("commit") == report.get("commit")
                    and (not row.get("merge_owner") or old.get("owner") in (None, row.get("merge_owner"))))
            rec = dict(old) if same else {k: old[k] for k in ("history",) if k in old}
            rec.update(name=row["name"], target=report["target"], commit=report.get("commit"),
                       entry=registry.get(row["name"]) or {})
            if not rec.get("takeover_at"):
                rec.update(owner=row.get("merge_owner"), takeover_at=row.get("takeover_at"))
                rec["due"] = rec["takeover_at"] + GRACE_S if rec["takeover_at"] else now
            outcome, kind, reason = assess(rec, row, now, p)
            row["outcome"] = outcome
            if outcome == "done" and not old:
                rows.append(row)
                continue
            was_pending = same and old.get("outcome") == "pending"
            _settle(rec, outcome, now)
            if outcome == "failed":
                rec.pop("closed", None)
                # Persist before posting: condition readers can immediately verify the alert.
                checks[key] = rec
                p.db.set_kv(CHECKS, checks)
                _alert(p, key, rec, kind, reason, now)
            elif outcome == "pending" and not was_pending:
                pending.append(f"{row['name']}: owner {rec['owner']}, takeover {timefmt.long(rec['takeover_at'], p)}")
            checks[key] = rec
            rows.append(row)
        p.db.set_kv(CHECKS, checks)
        if pending:
            p.db.post("out", f"tt-project {report['target']} deploy pending: " + "; ".join(pending)
                      + ". Follow-up checks run after takeover with a 5 min grace.", kind="info", severity="low")
    failed = any(r["outcome"] == "failed" for r in rows) or (not rows and report["rc"] != 0)
    return {**report, "projects": rows, "rc": int(failed)}


def tick(p, alert, now: float | None = None) -> None:
    """Called even while paused, once a minute per check. Close caught-up projects quietly; alert a
    daemon that stops or fails to come up, or a current upgrade error, at the next probe; alert a merge
    that missed its takeover only after its deadline. A resolved condition resets its deduplication,
    and its alert clears through holds."""
    now = time.time() if now is None else now
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        changed = False
        for key, rec in checks.items():
            if rec.get("closed") or now < float(rec.get("next_probe") or 0):
                continue
            rec["next_probe"] = now + PROBE_S
            changed = True
            outcome, kind, reason = assess(rec, _probe(rec, now), now, p)
            _settle(rec, outcome, now)
            if outcome == "failed":
                _alert(p, key, rec, kind, reason, now, alert)
        if changed:
            p.db.set_kv(CHECKS, checks)
