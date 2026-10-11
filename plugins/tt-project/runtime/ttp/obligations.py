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
again on a timer while the stall lasts; once every owner has fresh progress the episode ends, and a
later stall raises the next one.
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
    with p.db.tx():
        st = _load(p.db)
        oid = f"o{st['next']}"
        st["next"] += 1
        st["items"][oid] = {"id": oid, "text": text, "by": by, "created": now, "owners": {},
                            "receipt": None, "episode": 0, "raised": None}
        p.db.set_kv(KV, st)
    if owners:
        attach(p, oid, owners, now=now)
    return oid


def _owner_note(o: dict) -> str:
    return (f"## Obligation {o['id']} (urgent, owed to the user)\n{o['text']}\n"
            f"This task is a required owner. Record durable progress with evidence (a commit, a file, a URL) "
            f"at least every {CLOCK_S // 3600} h: `ttp obligation progress {o['id']} \"<evidence>\"`. Once "
            f"the deliverable exists and you checked it: `ttp obligation receipt {o['id']} \"<evidence>\"`. "
            f"Running, waiting or finishing this task without a receipt leaves it owed.")


def attach(p, oid: str, tids: list[int], now: float | None = None) -> list[int]:
    """Make tasks required owners; returns the ones newly attached. Each is told through its spec, and a
    running one also through its run's steer.md. An owner already attached keeps its clock."""
    from .coordinator import _append_update
    now = time.time() if now is None else now
    db = p.db
    new: list[int] = []
    with db.tx():
        st = _load(db)
        o = _need(st, oid)
        if o["receipt"]:
            raise ValueError(f"obligation {oid} already has a receipt")
        for tid in tids:
            t = db.task(int(tid))
            if not t:
                raise ValueError(f"no task #{tid}")
            if str(tid) in o["owners"]:
                continue
            if t["status"] in CLOSED:
                raise ValueError(f"task #{tid} is {t['status']}: attach the task that will produce it")
            o["owners"][str(tid)] = {"since": now, "progress": None, "evidence": []}
            if f"## Obligation {oid} " not in (t["spec"] or ""):
                db.update_task(int(tid), spec=(t["spec"] or "").rstrip() + "\n\n" + _owner_note(o) + "\n")
            new.append(int(tid))
        db.set_kv(KV, st)
    note = _owner_note(o)
    for tid in new:
        for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running' AND dir IS NOT NULL", (tid,)):
            _append_update(Path(r["dir"]) / "steer.md", note, f"obligation-{oid}-{tid}")
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
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
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


def overdue(o: dict, now: float) -> list[tuple[str, float]]:
    """(owner task id, or "" for no owner; seconds since its last durable progress) for each clock run out."""
    if o["receipt"]:
        return []
    if not o["owners"]:
        age = now - float(o["created"])
        return [("", age)] if age >= CLOCK_S else []
    out = []
    for tid, own in sorted(o["owners"].items(), key=lambda kv: int(kv[0])):
        age = now - float(own.get("progress") or own["since"])
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
    """Raise one keyed observation per obligation whose clock ran out (none again while that stall
    lasts; file_runs first). Returns the fingerprints raised."""
    now = time.time() if now is None else now
    db = p.db
    raised: list[str] = []
    with db.tx():
        st = _load(db)
        changed = False
        for o in st["items"].values():
            late = overdue(o, now)
            if not late:
                if o.get("raised"):
                    o["raised"] = None   # the stall is over: a later one is a new episode
                    changed = True
                continue
            if o.get("raised"):
                continue
            o["episode"] = int(o.get("episode") or 0) + 1
            o["raised"] = now
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
        else:
            late = {t for t, _ in overdue(o, now)}
            owners = ", ".join(
                f"#{t} {(now - float(w.get('progress') or w['since'])) / 3600:.1f} h"
                + (" OVERDUE" if t in late else "") for t, w in sorted(o["owners"].items(), key=lambda kv: int(kv[0])))
            state = "owed; " + (f"owners: {owners}" if owners else "no owner attached" + (" OVERDUE" if late else ""))
        out.append(f"{o['id']}  {o['text'][:100]}  [{state}]")
    return out
