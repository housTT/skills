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


def condition_key(name: str, version: str) -> str:
    return f"deploy:{name}:{version}"


def _probe(rec: dict, now: float) -> dict:
    return overview.deploy_status(rec["name"], rec["entry"], rec["target"], rec.get("commit"), now,
                                  project.hostname(), allow_newer=True)


def holds(db, arg: str, now: float) -> bool:
    rec = (db.kv(CHECKS) or {}).get("deploy:" + arg)
    return bool(rec and _probe(rec, now)["state"] != overview.OK)


def _alert(p, key: str, rec: dict, reason: str, alert=None) -> None:
    text = f"Deploy failed: {rec['name']} is not running tt-project {rec['target']}: {reason}."
    if alert:
        alert(key, text, severity="normal")
    elif not p.db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
        p.db.post("out", text, kind="alert", severity="normal", ref=key)


def record(p, report: dict, now: float | None = None) -> dict:
    """Classify done/pending/failed, accepting only live owned merges with a takeover deadline.

    Pending is success for after_push. Persist its original deadline across repeat deploys and
    daemon restarts; an active merge must not move that deadline each time it is checked.
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
            takeover = old.get("takeover_at") or row.get("takeover_at")
            owner = old.get("owner") or row.get("merge_owner")
            rec = {"name": row["name"], "target": report["target"], "commit": report.get("commit"),
                   "entry": registry.get(row["name"]) or {}, "owner": owner, "takeover_at": takeover,
                   "due": takeover + GRACE_S if takeover else now, **old}
            if row["state"] == overview.OK:
                row["outcome"] = "done"
                if old:
                    rec["closed"] = now
                    checks[key] = rec
            elif (row["state"] == overview.PENDING and row.get("daemon_alive") and
                  row.get("merge_pending") and row.get("merge_owner") and owner and takeover and
                  not row.get("upgrade_error") and now < rec["due"] and
                  not old.get("alerted")):
                row["outcome"] = "pending"
                rec.pop("closed", None)
                checks[key] = rec
                if not old:
                    pending.append(f"{row['name']}: owner {owner}, takeover {timefmt.long(takeover, p)}")
            else:
                rec.pop("closed", None)
                row["outcome"] = "failed"
                reason = row.get("upgrade_error") or row["reason"]
                if not row.get("daemon_alive"):
                    reason = f"daemon {row.get('daemon_state') or 'unverified'}; {reason}"
                if takeover and now >= rec["due"]:
                    reason += f"; missed takeover {timefmt.long(takeover, p)} (owner {owner})"
                elif row.get("merge_owner") and not takeover:
                    reason += "; no known takeover time"
                checks[key] = rec
                # Persist before posting: condition readers can immediately verify the alert.
                p.db.set_kv(CHECKS, checks)
                _alert(p, key, rec, reason)
                rec["alerted"] = old.get("alerted") or now
            rows.append(row)
        p.db.set_kv(CHECKS, checks)
        if pending:
            p.db.post("out", f"tt-project {report['target']} deploy pending: " + "; ".join(pending)
                      + ". Follow-up checks run after takeover with a 5 min grace.", kind="info", severity="low")
    failed = any(r["outcome"] == "failed" for r in rows) or (not rows and report["rc"] != 0)
    return {**report, "projects": rows, "rc": int(failed)}


def tick(p, alert, now: float | None = None) -> None:
    """Called even while paused. Close caught-up projects quietly, page once after their deadline.

    Failed projects are also checked, so their condition alerts clear when their daemons catch up.
    """
    now = time.time() if now is None else now
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        changed = False
        for key, rec in checks.items():
            if rec.get("closed") or now < float(rec.get("next_probe") or 0):
                continue
            rec["next_probe"] = now + PROBE_S
            changed = True
            row = _probe(rec, now)
            if row["state"] == overview.OK:
                rec["closed"] = now
            elif now >= rec["due"] and not rec.get("alerted"):
                at = timefmt.long(rec["takeover_at"], p) if rec.get("takeover_at") else "unknown"
                _alert(p, key, rec, f"missed takeover {at} (owner {rec.get('owner') or 'unknown'}); "
                       + row["reason"], alert)
                rec["alerted"] = now
        if changed:
            p.db.set_kv(CHECKS, checks)
