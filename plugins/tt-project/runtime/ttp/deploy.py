# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Post-push deploy outcomes and durable, model-free takeover checks.

Only the reporting project's database is written. Other projects are read through their daemon
receipts, never changed by verification. Ordinary `ttp deployed` remains a strict read-only probe.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

from . import overview, project, timefmt

CHECKS = "deploy_checks"
ALERTED = "deploy_alerted"   # the last deploy alert record() sent: pushq sends no after_push alert of its own
GRACE_S = 300
RESTART_WAIT_S = 900         # default delivery.deploy_restart_wait_s
PROBE_S = 60
HISTORY = 20
TAKEOVERS = "deploy_takeovers"   # project name -> the host-side takeover attempts of its overdue merge
TAKEOVER_TRIES = 3
TAKEOVER_SETTLE_S = 300          # after an attempt ends, its daemon has this long to report the release
TAKEOVER_TIMEOUT_S = 900         # an attempt running this long is over (stopped, when this daemon started it)
_children: dict[str, subprocess.Popen] = {}   # this daemon's own attempts, by project name


def condition_key(name: str, version: str) -> str:
    return f"deploy:{name}:{version}"


def release_key(version: str) -> str:
    """The one alert of a release's deploy, listing every project still behind."""
    return f"deploy:{version}"


def restart_wait(p) -> float:
    """delivery.deploy_restart_wait_s of the reporting project: how long a daemon may take to restart onto
    files already at the release before that counts as failed."""
    try:
        return float(project.push_queue_number("deploy_restart_wait_s",
                                               (p.config().get("delivery") or {}).get("deploy_restart_wait_s",
                                                                                      RESTART_WAIT_S)))
    except (ValueError, AttributeError, TypeError):
        return RESTART_WAIT_S


def _probe(rec: dict, now: float) -> dict:
    return overview.deploy_status(rec["name"], rec["entry"], rec["target"], rec.get("commit"), now,
                                  project.hostname(), allow_newer=True)


def _owned(row: dict) -> bool:
    """A step with a known owner: a merge held by a harness task, or a live daemon that restarts onto the
    new files (or completes its first tick on them) by itself."""
    return bool((row.get("merge_pending") and row.get("merge_owner")) or row.get("settling"))


def assess(rec: dict, row: dict, now: float, where=None) -> tuple[str, str, str]:
    """(outcome, kind, reason) of one probe: done; pending (an accepted owned merge before its deadline);
    or failed, kind "down" (a daemon that is down outside an upgrade) or "fault" (a current upgrade error):
    at once, whatever the deadline; "missed" (an accepted step past its deadline: a merge's takeover plus
    GRACE_S, a pending restart's bounded wait) or "unowned" (never accepted).
    Shared by record, tick and holds, so the alert clears exactly when its condition does. Times are
    shown in the zone of `where` (the reporting project)."""
    if row["state"] == overview.OK:
        return "done", "", ""
    reason = row["reason"]
    if row.get("upgrade_error"):
        return "failed", "fault", f"{row['upgrade_error']}; {reason}"
    if not row.get("daemon_alive") and not row.get("upgrading"):
        return "failed", "down", f"daemon {row.get('daemon_state') or 'unverified'}; {reason}"
    takeover = rec.get("takeover_at")
    if (_owned(row) and takeover) or rec.get("accepted"):
        # Accepted once, later steps of the same merge (files landed, a restart) wait for the same deadline.
        if now < rec["due"]:
            return "pending", "", reason
        at = timefmt.long(takeover, where) if takeover else "unknown"
        what = "still behind at its re-check" if rec.get("restart") else "missed takeover"
        return "failed", "missed", f"{what} {at} (owner {rec.get('owner') or 'unknown'}); {reason}"
    if row.get("merge_owner") and not takeover:
        reason += "; no known takeover time"
    return "failed", "unowned", reason


def holds(db, arg: str, now: float) -> bool:
    """The alert `deploy:<arg>` holds: for a release (`deploy:<version>`) while any project's check of it
    still fails; `deploy:<name>:<version>` (one project, as alerts before releases had one) while that does."""
    checks = db.kv(CHECKS) or {}
    takeovers = db.kv(TAKEOVERS) or {}
    recs = [checks.get("deploy:" + arg)] if ":" in arg else [r for r in checks.values() if r.get("target") == arg]
    return any(rec and not rec.get("closed") and _takeover(None, rec, *assess(rec, _probe(rec, now), now),
                                                           dict(takeovers.get(rec["name"]) or {}), now)[0] == "failed"
               for rec in recs)


def _owner_task(rec: dict) -> int | None:
    m = re.fullmatch(r"harness task #(\d+)", str(rec.get("owner") or ""))
    return int(m[1]) if m else None


def launch_takeover(p, name: str, task: int | None) -> subprocess.Popen:
    """Start the installed release's host-side `ttp upgrade <name>`, taking the merge over from harness task
    `task` (past its deadline), in a session of its own: a restart of this daemon leaves it running. Its
    output goes to this project's logs/takeover.log."""
    from .release import installed
    ttp = installed() / "bin" / "ttp"
    if not ttp.is_file():
        raise FileNotFoundError(f"no installed release at {ttp}")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "TTP_PROJECT", "TTP_TASK", "TTP_RUN_DIR")}
    argv = [sys.executable, str(ttp), "upgrade", name] + (["--take-over-from", str(task)] if task else [])
    p.logs.mkdir(parents=True, exist_ok=True)
    with open(p.logs / "takeover.log", "a") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} ttp upgrade {' '.join(argv[3:])}\n")
        log.flush()
        return subprocess.Popen(argv, cwd=str(p.base), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def _reap(name: str, t: dict, now: float) -> None:
    """Record the end of `name`'s running attempt: its exit code when this daemon started it, else once its
    process is gone (a restart or a reboot in between). An attempt past TAKEOVER_TIMEOUT_S is over."""
    pid = int(t.get("pid") or 0)
    if not pid:
        return
    child = _children.get(name)
    late = now - float(t.get("started") or 0) > TAKEOVER_TIMEOUT_S
    if child is not None and child.pid == pid:
        rc = child.poll()
        if rc is None and late:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            rc = "timed out"
        if rc is None:
            return
        _children.pop(name, None)
    else:
        from .locks import _alive
        if _alive(pid) and not late:
            return
        rc = "timed out" if late else "unknown (it ended while this daemon was down)"
    t.update(pid=None, ended=now, rc=rc if isinstance(rc, str) else f"exit {rc}")


def _takeover(p, rec: dict, outcome: str, kind: str, reason: str, t: dict, now: float,
              start: bool = False) -> tuple[str, str, str]:
    """A merge past its takeover deadline is taken over, not only reported: a host-side `ttp upgrade` runs
    (`start`, by the daemon's tick; started from the first probe past the deadline, also after a restart or
    a reboot), up to TAKEOVER_TRIES times, TAKEOVER_SETTLE_S apart. Meanwhile it is pending; it fails (and
    pages) only once every try ended and the project's own daemon still does not report the release.
    `t` is the project's attempt record, updated in place."""
    if outcome != "failed" or kind != "missed" or rec.get("restart"):
        return outcome, kind, reason
    tries = int(t.get("tries") or 0)
    if t.get("pid"):
        return "pending", "", f"host-side takeover running (try {tries} of {TAKEOVER_TRIES}); {reason}"
    if tries and now < float(t.get("ended") or 0) + TAKEOVER_SETTLE_S:
        return "pending", "", f"host-side takeover ended ({t.get('rc')}); its daemon is checked again; {reason}"
    if tries >= TAKEOVER_TRIES:
        return "failed", kind, (f"{reason}; host-side takeover tried {tries} time{'s' if tries > 1 else ''}, "
                                f"last {t.get('rc')} (see logs/takeover.log)")
    if not start:
        return "pending", "", f"host-side takeover due now; {reason}"
    try:
        child = launch_takeover(p, rec["name"], _owner_task(rec))
    except OSError as e:
        t.update(tries=TAKEOVER_TRIES, ended=None, rc=f"could not start ({e})")   # a new release tries again
        return "failed", kind, f"{reason}; host-side takeover could not start: {e}"
    _children[rec["name"]] = child
    t.update(tries=tries + 1, pid=child.pid, started=now, ended=None, rc=None)
    return "pending", "", f"host-side takeover started (try {tries + 1} of {TAKEOVER_TRIES}); {reason}"


def _mark(rec: dict, kind: str, now: float) -> bool:
    """Whether this failure is new news (deduplicated by the stored kind, which a resolved condition
    resets in `_settle`); records it and its history if so."""
    if rec.get("alerted") and rec.get("alerted_kind") == kind:
        return False
    rec.update(alerted=now, alerted_kind=kind)
    rec["history"] = (rec.get("history") or [])[-(HISTORY - 1):] + [{"at": now, "kind": kind, "commit": rec.get("commit")}]
    return True


def _alert(p, target: str, failed: list[tuple[dict, str, str]], alert=None) -> str:
    """One alert for a release's deploy, listing every project still behind (`failed`: (rec, kind, reason)).
    High only when a daemon is down; normal otherwise. Returns its key."""
    key = release_key(target)
    sev = "high" if any(kind == "down" for _, kind, _ in failed) else "normal"
    names = ", ".join(rec["name"] for rec, _, _ in failed)
    text = (f"Deploy failed: {names} {'is' if len(failed) == 1 else 'are'} not running tt-project {target}: "
            + "; ".join(f"{rec['name']}: {reason}" for rec, _, reason in failed) + ".")
    if alert:
        alert(key, text, severity=sev, every_s=0)
    elif not p.db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL", (key,)):
        p.db.post("out", text, kind="alert", severity=sev, ref=key)
    return key


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
    wait = restart_wait(p)
    pending, rows, failed = [], [], []
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        takeovers = p.db.kv(TAKEOVERS) or {}
        for row in report["projects"]:
            row = dict(row)
            key = condition_key(row["name"], report["target"])
            old = checks.get(key) or {}
            owner = row.get("owner") if row.get("settling") else row.get("merge_owner")
            same = (old and not old.get("closed") and old.get("commit") == report.get("commit")
                    and (not owner or old.get("owner") in (None, owner)))
            rec = dict(old) if same else {k: old[k] for k in ("history",) if k in old}
            rec.update(name=row["name"], target=report["target"], commit=report.get("commit"),
                       entry=registry.get(row["name"]) or {})
            if not rec.get("takeover_at") and row.get("settling"):
                # Its daemon restarts onto the files (or ticks on them) by itself: re-checked after the wait.
                rec.update(owner=owner, takeover_at=now + wait, due=now + wait, restart=True)
            elif not rec.get("takeover_at"):
                # The same owner still holding the merge of an earlier release keeps that release's deadline:
                # a task that holds every release in turn cannot push its takeover back with each one.
                earlier = [float(c["takeover_at"]) for c in checks.values() if c.get("name") == row["name"]
                           and not c.get("closed") and not c.get("restart") and c.get("takeover_at")
                           and owner and c.get("owner") == owner]
                at = row.get("takeover_at")
                rec.update(owner=row.get("merge_owner"), takeover_at=min([at] + earlier) if at else at)
                rec["due"] = rec["takeover_at"] + GRACE_S if rec["takeover_at"] else now
            if not same and not rec.get("restart"):
                takeovers.setdefault(row["name"], {})["tries"] = 0     # a new release gets its own tries
            outcome, kind, reason = _takeover(p, rec, *assess(rec, row, now, p),
                                              takeovers.setdefault(row["name"], {}), now)
            row["outcome"] = outcome
            if outcome == "done" and not old:
                rows.append(row)
                continue
            was_pending = same and old.get("outcome") == "pending"
            _settle(rec, outcome, now)
            if outcome == "failed":
                rec.pop("closed", None)
                failed.append((rec, kind, reason, _mark(rec, kind, now)))
            elif outcome == "pending" and not was_pending:
                at = timefmt.long(max(rec["takeover_at"], now), p)
                pending.append(f"{row['name']}: restart onto the new files pending, owner {rec['owner']}, "
                               f"re-checked {at}" if rec.get("restart") else
                               f"{row['name']}: owner {rec['owner']}, takeover {at}")
            checks[key] = rec
            rows.append(row)
        p.db.set_kv(CHECKS, checks)   # before the alert: condition readers can verify it at once
        p.db.set_kv(TAKEOVERS, takeovers)
        if failed:   # already alerted (still open), or alerted now: after_push's failure is this alert
            key = _alert(p, report["target"], [f[:3] for f in failed]) if any(new for *_, new in failed) \
                else release_key(report["target"])
            p.db.set_kv(ALERTED, {"key": key, "target": report["target"], "commit": report.get("commit"), "at": now})
        if pending:
            p.db.post("out", f"tt-project {report['target']} deploy pending: " + "; ".join(pending)
                      + ". Follow-up checks run after takeover with a 5 min grace, and after a pending "
                        "restart's wait.", kind="info", severity="low")
    failed = any(r["outcome"] == "failed" for r in rows) or (not rows and report["rc"] != 0)
    return {**report, "projects": rows, "rc": int(failed)}


def tick(p, alert, now: float | None = None) -> None:
    """Called even while paused, once a minute per check. Close caught-up projects quietly; alert a
    daemon that stops or fails to come up, or a current upgrade error, at the next probe; alert a merge
    that missed its takeover only after its deadline. A resolved condition resets its deduplication,
    and its alert clears through holds. One alert per release a probe finds newly failing, listing every
    project of that release still behind."""
    now = time.time() if now is None else now
    with p.db.tx():
        checks = p.db.kv(CHECKS) or {}
        takeovers = p.db.kv(TAKEOVERS) or {}
        for name, t in takeovers.items():
            _reap(name, t, now)
        changed = False
        failing: dict[str, list] = {}
        for key, rec in checks.items():
            if rec.get("closed") or now < float(rec.get("next_probe") or 0):
                continue
            rec["next_probe"] = now + PROBE_S
            changed = True
            outcome, kind, reason = _takeover(p, rec, *assess(rec, _probe(rec, now), now, p),
                                              takeovers.setdefault(rec["name"], {}), now, start=True)
            _settle(rec, outcome, now)
            if outcome == "failed":
                failing.setdefault(rec["target"], []).append((rec, kind, reason, _mark(rec, kind, now)))
        live = {rec["name"] for rec in checks.values() if not rec.get("closed")}
        takeovers = {n: t for n, t in takeovers.items() if n in live or t.get("pid")}
        if changed or takeovers != (p.db.kv(TAKEOVERS) or {}):
            p.db.set_kv(CHECKS, checks)
            p.db.set_kv(TAKEOVERS, takeovers)
        for target, failed in failing.items():
            if any(new for *_, new in failed):
                _alert(p, target, [f[:3] for f in failed], alert)
