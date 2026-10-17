"""Shared check failures: a waiting task whose own detached `ttp checks` failed only on tests that
also fail elsewhere is held asleep instead of woken at its tier (Daemon._probe_passed).

A failure is shared when every failing pytest id (the failing command's `FAILED`/`ERROR path::name`
lines, complete against pytest's summary counts) also failed in the checks of at least
`checks.shared_min_tasks` other changes (lineages: one change's code, review, review-fix and
`continue:` tasks are one, and its own never count) on other heads within
`checks.shared_window_h`, or the push queue's known-bad tip lists it. Any other runner, an incomplete list or one id not shared wakes the task as before.
A held task's checks run again on its same head, model-free, once the push-branch tip moves past a
commit touching a failing test's file, or after `checks.shared_backoff_h`; after RERUNS_MAX reruns
it wakes. One incident per shared failure carries one fix task, one low feed line, the tasks held
and the wakes avoided with their estimated $ (the daily audit's line). A task still lands only after
its own checks pass: nothing here skips or weakens them."""
from __future__ import annotations

import json
import re
import sqlite3

from .db import TERMINAL_TASK_STATES, DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks_failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL, head TEXT NOT NULL, ts REAL NOT NULL,
  ids TEXT NOT NULL, root INTEGER);
CREATE INDEX IF NOT EXISTS checks_failures_ts ON checks_failures(ts);
CREATE TABLE IF NOT EXISTS checks_incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ids TEXT NOT NULL, opened REAL NOT NULL, last REAL,
  closed REAL, fix_task INTEGER, tasks TEXT NOT NULL DEFAULT '[]', held INTEGER NOT NULL DEFAULT 0,
  avoided_usd REAL NOT NULL DEFAULT 0);
"""
MIN_TASKS = 2          # checks.shared_min_tasks: other changes (lineages) that must have failed each id
WINDOW_H = 6.0         # checks.shared_window_h: how far back their failures count
BACKOFF_H = 2.0        # checks.shared_backoff_h: a held task's checks run again after this at the latest
RERUNS_MAX = 3         # reruns on one head before the task wakes as before
KEEP_S = 7 * 86400     # failure rows kept this long
# A review the push queue failed on its batch checks (review_failure check_only, pushq._settle):
# the failing ids, and whether they are all of them (failing_ids).
FAILING_KEY, COMPLETE_KEY = "failing_tests", "failing_tests_complete"

_ID = re.compile(r"^(?:FAILED|ERROR) (\S+::[^\s\[]*(?:\[.*?\])?)(?=\s|$)", re.M)
# pytest's last summary line: bare under -q, wrapped in '=' at the default verbosity.
_SUMMARY = re.compile(r"^(?:=+ )?(\d+ [a-z]+(?:, \d+ [a-z]+)* in [\d.]+s\b[^=\n]*?)(?: =+)?\s*$", re.M)


def settings(cfg: dict) -> dict:
    c = cfg.get("checks") if isinstance(cfg.get("checks"), dict) else {}
    return {"min_tasks": int(c.get("shared_min_tasks", MIN_TASKS)),
            "window_s": float(c.get("shared_window_h", WINDOW_H)) * 3600,
            "backoff_s": float(c.get("shared_backoff_h", BACKOFF_H)) * 3600}


def failing_ids(output: str) -> tuple[list[str], bool]:
    """The failing pytest ids in a check's output, and whether they are all of them: pytest's last
    summary line (bare or in '=' bars) counts as many failed and errors as there are FAILED/ERROR
    lines. Any other
    runner's output gives ([], False)."""
    lines = _ID.findall(output or "")
    ids = sorted(set(lines))
    summaries = _SUMMARY.findall(output or "")
    if not ids or not summaries:
        return ids, False
    last = summaries[-1]
    n = sum(int(m.group(1)) for m in re.finditer(r"\b(\d+) (?:failed|errors?)\b", last))
    return ids, n == len(lines)


def ensure(db: DB) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.x(stmt)
    if "root" not in {r["name"] for r in db.q("PRAGMA table_info(checks_failures)")}:
        db.x("ALTER TABLE checks_failures ADD COLUMN root INTEGER")


def root(db: DB, task: int) -> int:
    """The first task of `task`'s lineage (chaincap.chain: `continues:` and `continue:` hops), itself
    when it has none. A review maps to its subject's root (db.review_subject, through re-reviews), so
    one change's code, fix, continue and review tasks share it."""
    from .chaincap import MAX_CHAIN, chain, prev_of
    from .db import review_subject
    seen: set[int] = set()
    cur = task
    while cur not in seen and len(seen) < MAX_CHAIN:
        seen.add(cur)
        t = db.task(cur)
        if t and t["kind"] == "review":
            if (s := review_subject(t)) is None:
                return cur
            cur = s
            continue
        ch = chain(db, cur)
        if not ch:
            return cur
        first = ch[0]
        nxt = prev_of(first)   # chain stops at a review hop: go on through its subject
        if nxt is None or nxt in seen or not (pt := db.task(nxt)) or pt["kind"] != "review":
            return int(first["id"])
        seen.update(int(h["id"]) for h in ch)
        cur = nxt
    return cur


def record(db: DB, task: int, head: str, ids: list[str], now: float) -> None:
    """One task's failing ids on one head (each head once), with its lineage's root."""
    ensure(db)
    db.x("DELETE FROM checks_failures WHERE ts<?", (now - KEEP_S,))
    if not db.one("SELECT id FROM checks_failures WHERE task=? AND head=? AND ids=?", (task, head, json.dumps(ids))):
        db.x("INSERT INTO checks_failures(task,head,ts,ids,root) VALUES(?,?,?,?,?)",
             (task, head, now, json.dumps(ids), root(db, task)))


def shared(db: DB, task: int, head: str, ids: list[str], now: float, cfg: dict,
           tip_ids: list[str] | None = None) -> bool:
    """Every id failed in `min_tasks` other lineages (other changes), on other heads within the
    window, or on the tip."""
    if not ids:
        return False
    s = settings(cfg)
    ensure(db)
    seen: dict[str, set[int]] = {}   # id -> the other lineages (roots) it failed in
    for r in db.q("SELECT COALESCE(root, task) AS lineage, ids FROM checks_failures WHERE ts>=? AND task!=? AND head!=? "
                  "AND COALESCE(root, task)!=?", (now - s["window_s"], task, head, root(db, task))):
        try:
            got = json.loads(r["ids"])
        except ValueError:
            continue
        for i in got if isinstance(got, list) else []:
            seen.setdefault(i, set()).add(r["lineage"])
    on_tip = set(tip_ids or [])
    return all(i in on_tip or len(seen.get(i, ())) >= s["min_tasks"] for i in ids)


def files(ids: list[str]) -> list[str]:
    """The test files the ids name."""
    return sorted({i.split("::", 1)[0] for i in ids})


def _rows(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def incident(db: DB, task: dict, ids: list[str], excerpt: str, now: float, cfg: dict) -> tuple[int, bool]:
    """The open incident sharing an id with `ids` (closed once its fix task ended), extended with
    them and `task`, else a new one with its fix task and one low feed line. (id, opened now)."""
    ensure(db)
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NULL ORDER BY id"):
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix is None or fix["status"] in TERMINAL_TASK_STATES:
            db.x("UPDATE checks_incidents SET closed=? WHERE id=?", (now, r["id"]))
            continue
        have = _rows(r["ids"])
        if not set(have) & set(ids):
            continue
        tasks = _rows(r["tasks"])
        new_ids = sorted(set(have) | set(ids))
        db.x("UPDATE checks_incidents SET ids=?, tasks=?, last=? WHERE id=?",
             (json.dumps(new_ids), json.dumps(sorted(set(tasks) | {task["id"]})), now, r["id"]))
        if (new_ids != have or task["id"] not in tasks) and fix["status"] == "queued" and not fix["attempts"]:
            db.update_task(fix["id"], spec=_spec(new_ids, excerpt, sorted(set(tasks) | {task["id"]})))
        return r["id"], False
    tier = "standard"
    budget = float(((cfg.get("budget") or {}).get("task_default_usd") or {}).get(tier, 8.0))
    title = f"Fix shared check failure: {ids[0]}" + (f" (+{len(ids) - 1} more)" if len(ids) > 1 else "")
    fid = db.add_task(title[:200], _spec(ids, excerpt, [task["id"]]), kind="code", tier=tier, priority=2,
                      origin="daemon", budget_usd=budget)
    iid = db.x("INSERT INTO checks_incidents(ids,opened,last,fix_task,tasks) VALUES(?,?,?,?,?)",
               (json.dumps(ids), now, now, fid, json.dumps([task["id"]])))
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", "checks_shared", f"checks_shared:{iid}", "low",
          f"Shared check failure: {len(ids)} test(s) ({', '.join(ids[:3])}{', ...' if len(ids) > 3 else ''}) fail "
          f"in several tasks' checks. Their tasks sleep without a model run until the push-branch tip changes "
          f"those tests or a backoff passes; fix task #{fid}.", "handled", fid))
    return iid, True


def _spec(ids: list[str], excerpt: str, tasks: list[int]) -> str:
    return ("The same tests fail in the detached checks of several unrelated tasks on different heads, so the "
            "cause is shared (the push-branch tip, the environment or the tests themselves), not those tasks' "
            "changes. Those tasks sleep without model runs and rerun their checks once the push-branch tip "
            "changes these test files.\n\nFailing test ids:\n" + "\n".join(f"- {i}" for i in ids)
            + f"\n\nTasks held: {', '.join(f'#{t}' for t in tasks)}\n\nExcerpt of one failing run:\n```\n"
            + excerpt[-1500:] + "\n```\n\nReproduce on the push-branch tip, find the cause and fix it there "
            "(test, code or environment), with a test. Never skip or weaken the required checks.")


def covered(db: DB, ids: list[str], skip: set[int] | frozenset = frozenset()) -> str:
    """What already owns every id ("" when one is not owned): an open incident whose fix task is
    live, or a live code task (not in `skip`) whose title or spec names the id."""
    if not ids:
        return ""
    ensure(db)
    owners: list[str] = []
    left = set(ids)
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NULL ORDER BY id"):
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix and fix["status"] not in TERMINAL_TASK_STATES and left & set(_rows(r["ids"])):
            owners.append(f"incident {r['id']} (fix #{fix['id']})")
            left -= set(_rows(r["ids"]))
    if left:
        done = sorted(TERMINAL_TASK_STATES)
        live = [t for t in db.q("SELECT id, title, spec FROM tasks WHERE kind='code' AND status NOT IN "
                                f"({','.join('?' * len(done))}) ORDER BY id", done) if t["id"] not in skip]
        for i in sorted(left):
            t = next((t for t in live if i in f"{t['title'] or ''}\n{t['spec'] or ''}"), None)
            if t is None:
                return ""
            owners.append(f"#{t['id']}")
    return ", ".join(dict.fromkeys(owners))


def avoided(db: DB, iid: int, tid: int, usd: float) -> None:
    """One wake avoided for task `tid` in incident `iid`."""
    r = db.one("SELECT tasks FROM checks_incidents WHERE id=?", (iid,))
    tasks = sorted(set(_rows(r["tasks"] if r else "[]")) | {tid})
    db.x("UPDATE checks_incidents SET held=held+1, avoided_usd=avoided_usd+?, tasks=? WHERE id=?",
         (usd, json.dumps(tasks), iid))


def wake_usd(db: DB, tier: str, now: float) -> float:
    """A wake's estimated cost: the mean worker run of that tier over the last week."""
    r = db.one("SELECT AVG(r.cost_usd) a FROM runs r JOIN tasks t ON t.id=r.task WHERE t.tier=? AND "
               "r.role!='coordinator' AND r.started>=? AND r.cost_usd>0", (tier, now - KEEP_S))
    return float((r or {}).get("a") or 0.0)


def audit_line(db: DB, since: float) -> str:
    """The self-efficiency audit's line: incidents active in the window; '' with none."""
    try:
        rows = db.q("SELECT * FROM checks_incidents WHERE COALESCE(last, opened)>=? ORDER BY id", (since,))
    except sqlite3.OperationalError:   # no incident yet: the table is made with the first
        return ""
    if not rows:
        return ""
    held = {t for r in rows for t in _rows(r["tasks"])}
    return (f"shared check failures: {len(rows)} incident(s), {len(held)} task(s) held, "
            f"{sum(int(r['held']) for r in rows)} wakes avoided (~${sum(float(r['avoided_usd']) for r in rows):.2f}): "
            + "; ".join(f"fix #{r['fix_task']} {len(_rows(r['ids']))} test(s), {len(_rows(r['tasks']))} task(s)"
                        + (" (closed)" if r["closed"] else "") for r in rows[:8]))
