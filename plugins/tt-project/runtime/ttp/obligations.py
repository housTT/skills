# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent user obligations: a deliverable the user asked for stays owed until a receipt exists.

An obligation (`ttp obligation add "<what>" [--owner <task>]...`) is a deliverable the user is owed,
attached to the tasks meant to produce it, its required owners (`ttp obligation attach <id> <task>...`).
Attaching tells each owner: its spec gets a section naming the obligation and how to report on it, and
a running owner hears it at once through its steer.md. Attaching again changes nothing.

Each required owner has its own durable-progress clock (CLOCK_S, two hours). It starts when the task is
attached and is reset only by that owner recording durable progress with evidence (`ttp obligation
progress <id> "<commit, file or URL>"`). Nothing else resets it: task edits, token activity, the task
running or waiting, a re-attach. A receipt (`ttp obligation receipt <id> "<evidence>"`) ends the
obligation. An owner task that ends (done, failed, cancelled) without one leaves it owed and its clock
running. The harness records the evidence as given: checking it stays with the owner.

Inside a run the CLI cannot write the project's database, so `progress` and `receipt` append to the
run's RUN_FILE, and the daemon files them (only for an obligation that run's task owns). Outside a run
(the user, the relay) they are filed at once.

Daemon.check_obligations looks every CHECK_EVERY_S, model-free: it files new run records, and for each
open obligation with an owner past its clock (or no owner at all, past the clock from its creation)
raises one high observation keyed `obligation:<id>:<episode>` for the coordinator. It is not raised
again on a timer while the same owners stay stalled; an owner not yet covered that stalls (say one
attached after an earlier owner ended) opens the next episode, and once no owner is late the episode ends.
A mistaken or no longer wanted obligation is withdrawn with `ttp obligation drop <id> "<why>"`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

KV = "obligations"            # kv: {"next": n, "items": {id: record}, "runs": {run id: lines filed}}
RUN_FILE = "obligations.jsonl"
CLOCK_S = 2 * 3600
CHECK_EVERY_S = 300
RUN_FILES_DAYS = 7            # run records are looked for in runs started this recently (or still running)
CLOSED = ("done", "failed", "cancelled")
TEXT_MAX = 500
EVIDENCE_MAX = 300
EVIDENCE_KEPT = 10


def _load(db) -> dict:
    st = db.kv(KV) or {}
    st.setdefault("next", 1)
    st.setdefault("items", {})
    st.setdefault("runs", {})
    return st


def items(db) -> list[dict]:
    return sorted(_load(db)["items"].values(), key=lambda o: int(o["id"][1:]))


def get(db, oid: str) -> dict | None:
    return _load(db)["items"].get(oid)


def _need(st: dict, oid: str) -> dict:
    o = st["items"].get(oid)
    if not o:
        raise ValueError(f"no obligation {oid}; `ttp obligation list` shows them")
    return o


def add(p, text: str, owners: list[int] = (), by: str = "user", now: float | None = None) -> str:
    text = " ".join(text.split())[:TEXT_MAX]
    if not text:
        raise ValueError("an obligation needs a text: what is owed")
    now = time.time() if now is None else now
    with p.db.tx():   # one transaction with its owners: a refused owner leaves no obligation behind
        st = _load(p.db)
        oid = f"o{st['next']}"
        st["next"] += 1
        o = st["items"][oid] = {"id": oid, "text": text, "by": by, "created": now, "owners": {},
                                "receipt": None, "dropped": None, "episode": 0, "raised": None, "covered": []}
        _attach(p, o, owners, now)
        p.db.set_kv(KV, st)
    return oid


def drop(p, oid: str, why: str, by: str = "user", now: float | None = None) -> str:
    """Withdraw an obligation recorded by mistake or no longer wanted: it is not owed and raises nothing."""
    why = " ".join(str(why or "").split())[:EVIDENCE_MAX]
    if not why:
        raise ValueError("drop needs a reason: why it is no longer owed")
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:
            raise ValueError(f"obligation {oid} already has a receipt")
        if o.get("dropped"):
            return f"{oid} was already dropped"
        o["dropped"] = {"at": now, "by": by, "why": why}
        p.db.set_kv(KV, st)
    return f"{oid} dropped; it is no longer owed"


def _owner_note(o: dict) -> str:
    return (f"## Obligation {o['id']} (urgent, owed to the user)\n{o['text']}\n"
            f"This task is a required owner. Record durable progress with evidence (a commit, a file, a URL) "
            f"at least every {CLOCK_S // 3600} h: `ttp obligation progress {o['id']} \"<evidence>\"`. Once "
            f"the deliverable exists and you checked it: `ttp obligation receipt {o['id']} \"<evidence>\"`. "
            f"Running, waiting or finishing this task without a receipt leaves it owed.")


def attach(p, oid: str, tids: list[int], now: float | None = None) -> list[int]:
    """Make tasks required owners; returns the ones newly attached. Each is told through its spec, and a
    running one also through its run's steer.md. An owner already attached keeps its clock."""
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:
            raise ValueError(f"obligation {oid} already has a receipt")
        if o.get("dropped"):
            raise ValueError(f"obligation {oid} was dropped")
        new = _attach(p, o, tids, now)
        p.db.set_kv(KV, st)
    return new


def _attach(p, o: dict, tids, now: float) -> list[int]:
    """attach() inside the caller's transaction: every owner is checked before anything is written, and
    running owners hear it only once the transaction commits."""
    from .coordinator import _append_update
    db, oid = p.db, o["id"]
    tasks = {}
    for tid in tids:
        t = db.task(int(tid))
        if not t:
            raise ValueError(f"no task #{tid}")
        if str(tid) not in o["owners"] and t["status"] in CLOSED:
            raise ValueError(f"task #{tid} is {t['status']}: attach the task that will produce it")
        tasks[int(tid)] = t
    new: list[int] = []
    for tid, t in tasks.items():
        if str(tid) in o["owners"]:
            continue
        o["owners"][str(tid)] = {"since": now, "progress": None, "evidence": []}
        if f"## Obligation {oid} " not in (t["spec"] or ""):
            db.update_task(tid, spec=(t["spec"] or "").rstrip() + "\n\n" + _owner_note(o) + "\n")
        new.append(tid)
    note = _owner_note(o)

    def tell():
        for tid in new:
            for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running' AND dir IS NOT NULL", (tid,)):
                _append_update(Path(r["dir"]) / "steer.md", note, f"obligation-{oid}-{tid}")
    db.after_commit(tell)
    return new


def record(p, oid: str, kind: str, evidence: str, task: int | None, now: float | None = None) -> str:
    """File durable progress or a receipt. Progress counts only from a required owner, and resets only
    that owner's clock. Returns what was done, in a few words."""
    if kind not in ("progress", "receipt"):
        raise ValueError(f"unknown record kind {kind!r}")
    evidence = " ".join(str(evidence or "").split())[:EVIDENCE_MAX]
    if not evidence:
        raise ValueError(f"{kind} needs evidence: a commit, a file or a URL")
    now = time.time() if now is None else now
    with p.db.tx():
        st = _load(p.db)
        o = _need(st, oid)
        if o["receipt"]:
            return f"{oid} already has a receipt"
        if o.get("dropped"):
            return f"{oid} was dropped; nothing to record"
        own = o["owners"].get(str(task)) if task is not None else None
        if task is not None and own is None:
            raise ValueError(f"task #{task} is not a required owner of {oid}")
        if kind == "progress":
            if own is None:
                raise ValueError("progress is recorded by an owner task, from its run")
            own["progress"] = max(float(own.get("progress") or 0), now)
            own["evidence"] = (own.get("evidence") or [])[-(EVIDENCE_KEPT - 1):] + [{"at": now, "text": evidence}]
            msg = f"progress on {oid} recorded for #{task}"
        else:
            o["receipt"] = {"at": now, "task": task, "evidence": evidence}
            msg = f"{oid} has its receipt; it is no longer owed"
        p.db.set_kv(KV, st)
    return msg


def append_run(run_dir: Path, oid: str, kind: str, evidence: str, task: str) -> None:
    from .project import durable_append
    durable_append(Path(run_dir) / RUN_FILE, json.dumps({"ts": time.time(), "id": oid, "kind": kind,
                                                          "evidence": evidence, "task": task}) + "\n")


def file_runs(p, now: float | None = None) -> list[str]:
    """File the records runs wrote (RUN_FILE) that are not filed yet; returns a line per refused one.
    A record counts for the run's own task only, at the time it was written (never in the future)."""
    now = time.time() if now is None else now
    db = p.db
    seen = dict(_load(db)["runs"])
    out: list[str] = []
    runs = db.q("SELECT id, task, dir, started FROM runs WHERE dir IS NOT NULL AND task IS NOT NULL "
                "AND (status='running' OR started>=?)", (now - RUN_FILES_DAYS * 86400,))
    for r in runs:
        f = Path(r["dir"]) / RUN_FILE
        try:
            # Only whole lines: an append still being written is read on a later pass, not lost.
            lines = f.read_text(encoding="utf-8", errors="replace").split("\n")[:-1]
        except OSError:
            continue
        done = int(seen.get(str(r["id"]), 0))
        for line in lines[done:]:
            try:
                rec = json.loads(line)
                record(p, str(rec["id"]), str(rec["kind"]), str(rec.get("evidence") or ""), int(r["task"]),
                       now=min(float(rec.get("ts") or now), now))
            except (ValueError, KeyError, TypeError) as e:
                out.append(f"run {r['id']}: {str(e)[:200]}")
        seen[str(r["id"])] = len(lines)
    keep = {str(r["id"]) for r in runs}
    with db.tx():
        st = _load(db)
        st["runs"] = {k: v for k, v in seen.items() if k in keep}
        db.set_kv(KV, st)
    return out


def _last(own: dict) -> float:
    """When an owner's clock last started: its attach time, or its latest progress if later."""
    return max(float(own["since"]), float(own.get("progress") or 0))


def overdue(o: dict, now: float) -> list[tuple[str, float]]:
    """(owner task id, or "" for no owner; seconds since its last durable progress) for each clock run out."""
    if o["receipt"] or o.get("dropped"):
        return []
    if not o["owners"]:
        age = now - float(o["created"])
        return [("", age)] if age >= CLOCK_S else []
    out = []
    for tid, own in sorted(o["owners"].items(), key=lambda kv: int(kv[0])):
        age = now - _last(own)
        if age >= CLOCK_S:
            out.append((tid, age))
    return out


def anomaly_text(o: dict, late: list[tuple[str, float]], status: dict[str, str]) -> str:
    parts = []
    for tid, age in late:
        if not tid:
            parts.append(f"no owner task is attached ({age / 3600:.1f} h since it was recorded)")
            continue
        st = status.get(tid, "gone")
        tail = " without a receipt" if st in CLOSED or st == "gone" else ""
        parts.append(f"#{tid} ({st}{tail}) has no durable progress for {age / 3600:.1f} h")
    return (f"Obligation {o['id']} is still owed to the user: {o['text']}\n- " + "\n- ".join(parts)
            + f"\nGet it moving: attach or start the owner that produces it (`ttp obligation attach`), or "
              f"have the owner record progress or its receipt.")


def check(p, now: float | None = None) -> list[str]:
    """Raise one keyed observation per stall (file_runs first). An episode covers the owners late when it
    was raised: none again while they stay late, a new one when an owner it does not cover runs late, and
    it ends once no owner is late. Returns the fingerprints raised."""
    now = time.time() if now is None else now
    db = p.db
    raised: list[str] = []
    with db.tx():
        st = _load(db)
        changed = False
        for o in st["items"].values():
            late = overdue(o, now)
            late_ids = [t for t, _ in late]
            covered = [t for t in o.get("covered") or [] if t in late_ids]   # an owner back on time leaves it
            if not late:
                if o.get("raised") or o.get("covered"):
                    o["raised"], o["covered"] = None, []   # the stall is over: a later one is a new episode
                    changed = True
                continue
            if set(late_ids) <= set(covered):
                if covered != o.get("covered"):
                    o["covered"] = covered
                    changed = True
                continue
            o["episode"] = int(o.get("episode") or 0) + 1
            o["raised"], o["covered"] = now, late_ids
            changed = True
            fp = f"obligation:{o['id']}:{o['episode']}"
            if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
                continue
            ids = [int(t) for t, _ in late if t]
            status = {str(t["id"]): t["status"] for t in
                      db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "obligation_overdue", fp, "high", anomaly_text(o, late, status)[:4000], "queued",
                  ids[0] if ids else None))
            raised.append(fp)
        if changed:
            db.set_kv(KV, st)
    return raised


def lines(db, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    out = []
    for o in items(db):
        if o["receipt"]:
            state = f"received ({o['receipt']['evidence'][:80]})"
        elif o.get("dropped"):
            state = f"dropped ({o['dropped']['why'][:80]})"
        else:
            late = {t for t, _ in overdue(o, now)}
            owners = ", ".join(
                f"#{t} {(now - _last(w)) / 3600:.1f} h"
                + (" OVERDUE" if t in late else "") for t, w in sorted(o["owners"].items(), key=lambda kv: int(kv[0])))
            state = "owed; " + (f"owners: {owners}" if owners else "no owner attached" + (" OVERDUE" if late else ""))
        out.append(f"{o['id']}  {o['text'][:100]}  [{state}]")
    return out
