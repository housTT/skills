# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Time to recover (TTR) per machine: model-free incident records and their report.

An incident starts when a machine this project watches goes down and ends when nothing says so any
more. A project watches the machines its charter's Resources name, those it is the recovery owner or
fallback of, and those its own heal checks name. A machine is down while any of these says so:
- one of this project's heal checks reports it to the machine ledger (`machine_reported`: down or
  held, past the check's own rules for unknown results, findings and known faults; heal.py), from
  the check's first unhealthy run (`since`);
- a co-tenant's condition on it in the shared routes file (machine_ledger.report) seen within
  machine_ledger.CONDITION_GONE_S, from its `first` report;
- a known outage recorded for it (`ttp machines outage`), from when it was recorded.
The earliest of these is the incident's start, and its kind (down, held or outage) and cause. Its end
is the first daemon tick (every OBSERVE_S) that finds none of them; a later tick that finds one again
starts a new incident, never before the last one ended. When the open incident's source recovers but
another of a different kind and cause (say a known outage after a heal check) keeps the machine down,
the incident hands over: it ends when the new source began (no earlier than it started) and a new one
opens from there with the new kind and cause, so each source's incident keeps its own start and
cause. A source changing only its kind (a heal check's down to held) or only its cause keeps it.

Records live in this project's database (ttr_incidents): rows are only added, and each row's end is
written once. A unique index allows one open incident per machine and every step is one transaction,
so a daemon restart or host reboot mid-incident neither loses nor duplicates one: the next tick finds
the open row and keeps it, or ends it once the machine is healthy. While the daemon is stopped nothing
is observed: a recovery in that time is recorded at its first tick after. While the project is paused
heal checks do not run, so their reports freeze: a heal report whose last check is older than the
check's period plus HEAL_STALE_MARGIN_S is ignored then, and an incident only it kept open ends there;
co-tenant conditions and known outages still count.

`ttp ttr [<project>] [<alias>] [--days N] [--json]` reports per machine the incidents in the window
(those still open, or that ended within it), the median, p90 and max TTR of the ended ones, the open
ones and the last one; the daily review carries one line per machine with an incident in REVIEW_DAYS.
"""
from __future__ import annotations

import math
import statistics
import time

from . import timefmt
from .db import DB

OBSERVE_S = 60          # how often the daemon records
DAYS = 30               # `ttp ttr` window by default
REVIEW_DAYS = 7         # the daily review's window
CAUSE_CHARS = 200
HEAL_STALE_MARGIN_S = 300   # while paused, a heal report older than its check's period plus this is ignored
KINDS = ("down", "held", "outage")


def watched(p) -> set[str]:
    """The machine aliases `p` records incidents for (module doc)."""
    from . import heal, machine_ledger, machines
    known = machines.load()
    mine = set(machine_ledger.resource_machines(p))
    mine |= {a for a, m in known.items() if p.name in (m.get("recovery_owner"), m.get("recovery_fallback"))}
    for _, spec in heal.checks(p.db):
        alias = heal.machine_of(spec, known)
        if alias:
            mine.add(alias)
    return mine


def down_now(p, now: float, aliases: set[str] | None = None) -> dict[str, dict]:
    """{alias: {"since", "kind", "cause"}} of the watched machines that are down now, by the earliest source."""
    from . import heal, machine_ledger
    aliases = watched(p) if aliases is None else aliases
    seen: dict[str, list[dict]] = {}
    paused = bool(p.db.kv("paused", False))
    for row, _ in heal.checks(p.db):
        st = heal.state(p.db, row["name"])
        rep = st.get("machine_reported")
        # Heal checks do not run while the project is paused: a report no newer than the check's
        # period plus a margin is frozen, not evidence the machine is still down.
        if paused and now - float(st.get("last_check") or 0) > float(row["every_s"] or 0) + HEAL_STALE_MARGIN_S:
            continue
        if isinstance(rep, list) and len(rep) == 2 and rep[0] in aliases:
            seen.setdefault(rep[0], []).append({"since": float(st.get("since") or st.get("unknown_since") or now),
                                                "kind": rep[1], "cause": f"heal check {row['name']}"})
    for c in machine_ledger.conditions().values():
        alias = c.get("alias")
        if alias in aliases and now - float(c.get("last") or 0) < machine_ledger.CONDITION_GONE_S:
            who = ", ".join(c.get("seen_by") or []) or "?"
            seen.setdefault(alias, []).append({"since": float(c.get("first") or now), "kind": c.get("condition"),
                                               "cause": f"reported by {who}: {c.get('text') or ''}".rstrip(": ")})
    for alias, o in machine_ledger.outages(now).items():
        if alias in aliases:
            seen.setdefault(alias, []).append({"since": float(o.get("since") or now), "kind": "outage",
                                               "cause": f"known outage: {o.get('why') or ''}"})
    out = {}
    for alias, recs in seen.items():
        first = min(recs, key=lambda r: r["since"])
        out[alias] = {**first, "since": min(first["since"], now),
                      "cause": " ".join(str(first["cause"]).split())[:CAUSE_CHARS]}
    return out


def observe(p, now: float | None = None) -> tuple[list[str], list[str]]:
    """One model-free step: open an incident for each watched machine newly down, end each open one
    whose machine is healthy again. Returns (opened, ended) aliases."""
    now = time.time() if now is None else now
    db: DB = p.db
    aliases = watched(p)
    was_open = {r["alias"] for r in db.q("SELECT alias FROM ttr_incidents WHERE ended IS NULL")}
    down = down_now(p, now, aliases | was_open)
    opened, ended = [], []
    with db.tx():
        open_rows = {r["alias"]: r for r in db.q("SELECT * FROM ttr_incidents WHERE ended IS NULL")}
        for alias, r in list(open_rows.items()):
            d = down.get(alias)
            if d is None:
                end = now
            elif d["kind"] != r["kind"] and d["cause"] != r["cause"]:
                end = min(max(d["since"], float(r["started"])), now)   # handover to another source
            else:
                continue
            if db.conn.execute("UPDATE ttr_incidents SET ended=? WHERE id=? AND ended IS NULL",
                               (end, r["id"])).rowcount:
                ended.append(alias)
                if d is not None:
                    del open_rows[alias]
        for alias, d in sorted(down.items()):
            if alias in open_rows:
                continue
            last = db.one("SELECT MAX(ended) AS e FROM ttr_incidents WHERE alias=?", (alias,))
            start = max(d["since"], float(last["e"] or 0) if last else 0.0)
            if db.conn.execute("INSERT OR IGNORE INTO ttr_incidents(alias, kind, cause, started) VALUES(?,?,?,?)",
                               (alias, d["kind"], d["cause"], start)).rowcount:
                opened.append(alias)
    return opened, ended


# report -------------------------------------------------------------------------------------------

def _p90(xs: list[float]) -> float:
    """Nearest-rank 90th percentile of a non-empty list."""
    s = sorted(xs)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def report(db: DB, alias: str | None = None, days: float = DAYS, now: float | None = None) -> dict[str, dict]:
    """{alias: stats} of the incidents open now or ended within `days` (module doc). TTRs in seconds."""
    now = time.time() if now is None else now
    since = now - days * 86400
    rows = db.q("SELECT * FROM ttr_incidents WHERE (ended IS NULL OR ended>=?)" + (" AND alias=?" if alias else "")
                + " ORDER BY started, id", (since, alias) if alias else (since,))
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["alias"], []).append(r)
    stats = {}
    for a, rs in sorted(out.items()):
        ttrs = [float(r["ended"]) - float(r["started"]) for r in rs if r["ended"] is not None]
        stats[a] = {
            "incidents": len(rs),
            "recovered": len(ttrs),
            "median_s": statistics.median(ttrs) if ttrs else None,
            "p90_s": _p90(ttrs) if ttrs else None,
            "max_s": max(ttrs) if ttrs else None,
            "open": [{"id": r["id"], "kind": r["kind"], "cause": r["cause"], "started": r["started"],
                      "open_s": now - float(r["started"])} for r in rs if r["ended"] is None],
            "last": {k: rs[-1][k] for k in ("id", "kind", "cause", "started", "ended")},
        }
    return stats


def _dur(s: float) -> str:
    if s >= 86400:
        return f"{s / 86400:.1f} d"
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def line(alias: str, s: dict, where, days: float) -> str:
    """One machine's stats as one line, times in the zone of `where` (a Project, settings or zone name)."""
    text = f"{alias}: {s['incidents']} incident{'s' if s['incidents'] != 1 else ''} in {days:g} d"
    if s["recovered"]:
        text += f"; TTR median {_dur(s['median_s'])}, p90 {_dur(s['p90_s'])}, max {_dur(s['max_s'])}"
    if s["open"]:
        text += "; open: " + ", ".join(f"{o['kind']} since {timefmt.long(o['started'], where)} "
                                       f"({_dur(o['open_s'])})" for o in s["open"])
    last = s["last"]
    if last["ended"] is not None:
        text += (f"; last: {last['kind']} {timefmt.long(last['started'], where)}, recovered in "
                 f"{_dur(float(last['ended']) - float(last['started']))}")
    return text + (f" ({last['cause']})" if last.get("cause") else "")


def lines(db: DB, where, alias: str | None = None, days: float = DAYS, now: float | None = None) -> list[str]:
    return [line(a, s, where, days) for a, s in report(db, alias, days, now).items()]


def review_lines(p, now: float | None = None) -> list[str]:
    """For the daily review: one line per machine with an incident in the last REVIEW_DAYS."""
    return lines(p.db, timefmt.home(p), days=REVIEW_DAYS, now=now)
