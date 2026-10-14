# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared machines: one recovery owner per machine, and a change ledger every co-tenant sees.

Several projects of one user may use the same machine (each lists it in its charter's Resources: its
co-tenants). Two things keep them from working against each other:

Recovery owner. machines.json gives a machine a `recovery_owner` project and a `recovery_fallback`
(`ttp machines set <alias> --owner <project> [--fallback <project>]`). A co-tenant that sees the machine
down or held past its grace (a command watcher's observation with `machine` and `condition`, or `report`)
records the condition in the shared routes file; the first daemon to tick routes it, model-free, to the
owner: a high-severity note in its inbox (`upstream.send`), or an event when the owner is the daemon's own
project, which the coordinator takes at high effort. When the owner cannot act (its daemon stopped or
stale, the project paused, its coordinator logged out or failing) for UNAVAILABLE_S, the condition goes to
the fallback, then to the user as an outage. Before each page, the ledger reads the owner's (then the
fallback's) own heal checks naming the machine, read-only (responsibilities.healthy_checks): while one that
observes the condition ran healthy and fresh (any such check for `down`, only a broker check for `held`)
and that project does not itself report the condition, the page is held, and that project gets one low
note per episode naming the reporting projects (for `down`, likely their own route problem).
A condition no co-tenant reported for CONDITION_GONE_S, or reported cleared, is over. With no owner set nothing is routed: each co-tenant's digest names the gap once.

Quiet outages (quiet_reason). Nobody is paged about a condition the harness already knows: a known outage
recorded with `ttp machines outage <alias> "<why>" [--owner <who>] --expires <t>` (a hardware repair, often
owned by a human or an external ticket), or an open change that expects the condition (`--expect down`).
The owner's first page still goes, but nothing escalates to the fallback or the user, while: the user paused
a resource on the machine, a reporting heal check carries a `known_fault`, or no open task of any project on
this machine uses it (a task's resource on it that its project paused does not count). A reporter this
machine cannot read counts as needing it. Each such outage is recorded once per machine (`quiet`), for the
daily review (review_lines), never alerted; its user alert clears. Once the reason ends, the steps resume
from that moment.

Change ledger. Every live change to a shared machine (a drop-in, a disabled recovery switch, a pause) is
recorded with `ttp machines change add <alias> "<what>" --undo "<cmd>" (--expires <t> | --until-probe
"<cmd>")`: who made it (the project), why (the text), how to undo it and when it ends. Each co-tenant's
digest lists the open entries for machines in its Resources, one line each. An entry is overdue when it
expires or its until-probe passes (exit 0: what it waited for is over, so the undo is due); a probe-only
entry also expires after PROBE_END_S. An overdue entry pages the project that made it, then the machine's
owner, then its fallback, each after the one before cannot act for UNAVAILABLE_S or let REPAGE_S pass,
then the user. Nothing is undone automatically: whoever is paged runs the undo and closes the entry
(`ttp machines change close <id>`). Probes run only in the daemon of the project that made the entry,
from its root, as end probes do. `ttp machines change edit <id> [--what] [--undo] [--expires | --until-probe]`
corrects an open entry in place (same id), with an edit trail in the entry.

Planned power cycles. `ttp machines change add <alias> "<why>" --power-cycle-at <t>` records a power-cycle
intent: an entry of kind `power_cycle` that needs no undo and closes itself POWER_CYCLE_AFTER_S after T
(never overdue; any co-tenant daemon closes it once past). It expects the machine `down` only from
POWER_CYCLE_LEAD_S before T until it ends (expects_now), so it never hides an outage outside the cycle. From POWER_CYCLE_LEAD_S before T until as long after it, each
daemon tells its running workers on that machine (the daemon's own host, or a task resource on it) once,
through their steer.md, to commit and hand off now (daemon.power_cycle_notices).

Both files live next to machines.json, written whole under one lock, so co-tenant daemons and CLIs never
lose each other's writes. Other projects' state is only read (their database, read-only), never written.

A page is never lost nor sent twice: `route` records each due page as pending, with a text fixed for its
step, and hands it to one daemon at a time (a claim that lapses after CLAIM_S); that daemon marks it
`delivered` only once it went out, or `release`s it for the next tick. A crash in between leaves it
pending, and the resend is idempotent (same inbox text, same event fingerprint, same alert episode).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import ends, machines, project

LEDGER = "machine-changes.json"
ROUTES = "machine-routes.json"
LOCK = "machine-ledger.lock"
CLAIM_S = 120               # a pending page one daemon took and did not mark delivered is retried after this
UNAVAILABLE_S = 1800         # a target that cannot act this long is passed over
REPAGE_S = 86400             # an item still open this long after its page is paged again, one step on
GRACE_S = 1800               # default: how long a condition lasts before it is routed
CONDITION_GONE_S = 3 * 3600  # a condition nobody reported this long is over
PROBE_END_S = 7 * 86400      # an entry with only an until-probe is overdue after this anyway
CLOSED_KEPT_S = 30 * 86400   # closed entries kept this long
TEXT_CHARS = 300
CONDITIONS = ("down", "held")
QUIET_KEPT_S = 2 * 86400     # a quiet outage's daily-review record is kept this long after it was last seen
REVIEW_S = 86400             # the daily review lists the quiet outages seen within this
GAP_KV = "machine_owner_gap_named"
POWER_CYCLE = "power_cycle"
POWER_CYCLE_LEAD_S = 600     # running workers on the machine are told to hand off this long before the cycle
POWER_CYCLE_AFTER_S = 3600   # a power-cycle intent closes itself this long after its time
USER = "the user"


def _path(name: str) -> Path:
    return project.HOME_DIR / name


@contextlib.contextmanager
def _locked():
    """The one lock both files are read and written under."""
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(name: str, empty: dict) -> dict:
    try:
        doc = json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return json.loads(json.dumps(empty))
    return doc if isinstance(doc, dict) else json.loads(json.dumps(empty))


def _write(name: str, doc: dict) -> None:
    project.write_json(_path(name), doc, 0o600)


def _one_line(text: Any, chars: int = TEXT_CHARS) -> str:
    return " ".join(str(text or "").split())[:chars]


# ledger -------------------------------------------------------------------------------------------

def add_change(alias: str, what: str, undo: str, by: str, expires: Any = None, until_probe: str | None = None,
               task: int | None = None, now: float | None = None, expects=None, extra: dict | None = None) -> dict:
    """Record a live change to machine `alias` made by project `by`; returns the entry. `expects`: the
    event fingerprints or machine conditions (down, held) the change is known to cause, which then
    keep the coordinator's turn routine (coordinator._known_case). `extra`: more fields (a kind)."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    what, undo, probe = _one_line(what), _one_line(undo, 1000), _one_line(until_probe, ends.PROBE_CHARS)
    if not what or not undo:
        raise ValueError("say what changed and give the --undo command")
    if not expires and not probe:
        raise ValueError("give an end: --expires <delay or time> or --until-probe <command>")
    if probe:
        ends.check_sh(probe, "--until-probe")
    end = float(expires) if isinstance(expires, (int, float)) else \
        ends.parse_expires(expires, now) if expires else now + PROBE_END_S   # a number: an epoch time
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        n = int(doc.get("next") or 1)
        entry = {"id": n, "alias": alias, "what": what, "undo": undo, "by": by, "task": task, "added": now,
                 "expires": end, "probe": probe or None, **(extra or {})}
        if expects:
            entry["expects"] = [_one_line(x, 200) for x in expects if _one_line(x, 200)]
        doc["next"] = n + 1
        doc["changes"] = [c for c in doc.get("changes", []) if not c.get("closed")
                          or now - float(c["closed"]) < CLOSED_KEPT_S] + [entry]
        _write(LEDGER, doc)
    return entry


def add_power_cycle(alias: str, at: Any, by: str, why: str = "", task: int | None = None,
                    now: float | None = None) -> dict:
    """Record a planned power cycle of `alias` at `at` (a delay or a time, as --expires takes)."""
    now = time.time() if now is None else now
    when = ends.parse_expires(at, now, what="--power-cycle-at")
    what = f"power-cycle intent, at {ends.shown(when)}" + (f": {_one_line(why)}" if _one_line(why) else "")
    return add_change(alias, what, "none: a planned power cycle; the entry closes itself after it", by,
                      expires=when + POWER_CYCLE_AFTER_S, task=task, now=now, expects=["down"],
                      extra={"kind": POWER_CYCLE, "at": when})


def power_cycles_due(now: float | None = None) -> list[dict]:
    """Open power-cycle intents whose time is within POWER_CYCLE_LEAD_S of now, either side."""
    now = time.time() if now is None else now
    return [c for c in changes() if c.get("kind") == POWER_CYCLE
            and abs(float(c.get("at") or 0) - now) <= POWER_CYCLE_LEAD_S]


def changes(aliases=None, open_only: bool = True) -> list[dict]:
    doc = _read(LEDGER, {"changes": []})
    return [c for c in doc.get("changes", []) if isinstance(c, dict) and (not open_only or not c.get("closed"))
            and (aliases is None or c.get("alias") in aliases)]


def close_change(cid: int, why: str = "", by: str = "", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        for c in doc.get("changes", []):
            if c.get("id") == cid and not c.get("closed"):
                c.update(closed=now, closed_why=_one_line(why) or "closed", closed_by=by)
                _write(LEDGER, doc)
                return c
    raise ValueError(f"no open machine change #{cid}")


def edit_change(cid: int, by: str, what: str | None = None, undo: str | None = None, expires: Any = None,
                until_probe: str | None = None, now: float | None = None) -> dict:
    """Correct open entry #cid in place: its text, undo command or end (--expires or --until-probe, as
    add_change takes them). Keeps its id, alias, author and creation time; records who edited it, when
    and the values it replaced in `edits`. A new end starts afresh (`page_from`): its until-probe result and overdue
    pages are dropped. Refuses a closed or unknown id, a power-cycle intent's undo or end, and an edit
    that changes nothing."""
    now = time.time() if now is None else now
    if expires and until_probe:
        raise ValueError("give one end: --expires or --until-probe")
    new: dict[str, Any] = {}
    if what is not None:
        new["what"] = _one_line(what)
    if undo is not None:
        new["undo"] = _one_line(undo, 1000)
    if not all(new.values()):
        raise ValueError("the text and the --undo command cannot be empty")
    if until_probe is not None:
        probe = _one_line(until_probe, ends.PROBE_CHARS)
        if not probe:
            raise ValueError("the --until-probe command cannot be empty")
        ends.check_sh(probe, "--until-probe")
        new.update(probe=probe, expires=now + PROBE_END_S)
    elif expires:
        new.update(probe=None, expires=float(expires) if isinstance(expires, (int, float))
                   else ends.parse_expires(expires, now))
    if not new:
        raise ValueError("say what to change: --what, --undo, --expires or --until-probe")
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid), None)
        if c is None:
            raise ValueError(f"no machine change #{cid}")
        if c.get("closed"):
            raise ValueError(f"machine change #{cid} is closed; record a new one with `ttp machines change add`")
        if c.get("kind") == POWER_CYCLE and set(new) - {"what"}:
            raise ValueError(f"machine change #{cid} is a planned power cycle: only --what can change; "
                             f"close it and record a new one for another time")
        if until_probe is not None and new["probe"] == c.get("probe"):
            del new["probe"], new["expires"]   # the same until-probe keeps its end
        was = {k: c.get(k) for k, v in new.items() if c.get(k) != v}
        if not was:
            raise ValueError(f"machine change #{cid} already says that: nothing to change")
        for k in was:
            c[k] = new[k]
        if "expires" in was:
            for k in ("probe_passed", "page"):
                c.pop(k, None)
            c["page_from"] = now   # a new page episode: its pages get ids of their own, not the delivered ones'
        c.setdefault("edits", []).append({"at": now, "by": by, "was": was})
        _write(LEDGER, doc)
        return c


def overdue(c: dict, now: float | None = None, where=None) -> str:
    """Why entry `c` is overdue ("" when it is not)."""
    now = time.time() if now is None else now
    if c.get("closed") or c.get("kind") == POWER_CYCLE:   # a power cycle has nothing to undo: it closes itself
        return ""
    if c.get("probe_passed"):
        return "its until-probe passed"
    if float(c.get("expires") or 0) <= now:
        return f"expired {ends.shown(float(c['expires']), where)}"
    return ""


def expects_now(c: dict, now: float | None = None) -> list[str]:
    """What entry `c` expects at `now`: nothing once it is closed or overdue, and for a power-cycle intent
    nothing outside its window (POWER_CYCLE_LEAD_S before T until its end), so it never quiets an outage
    days before the cycle, or after it when nobody closed it."""
    now = time.time() if now is None else now
    if c.get("closed") or overdue(c, now):
        return []
    if c.get("kind") == POWER_CYCLE:
        at = float(c.get("at") or 0)
        if not at - POWER_CYCLE_LEAD_S <= now < float(c.get("expires") or at + POWER_CYCLE_AFTER_S):
            return []
    return [str(x) for x in c.get("expects") or []]


def change_line(c: dict, now: float | None = None, where=None) -> str:
    late = overdue(c, now, where)
    end = f"until-probe `{c['probe']}`, at the latest {ends.shown(c['expires'], where)}" if c.get("probe") \
        else f"ends {ends.shown(c['expires'], where)}"
    by = c.get("by", "?") + (f" #{c['task']}" if c.get("task") else "")
    end += f"; expects {', '.join(c['expects'])}" if c.get("expects") else ""
    return (f"#{c['id']} {c['alias']}: {c['what']} (by {by}; undo: `{c['undo']}`; {end})"
            + (f" OVERDUE: {late}" if late else ""))


# conditions ---------------------------------------------------------------------------------------

def report(by: str, alias: str, condition: str, text: str = "", grace_s: float | None = None,
           cleared: bool = False, now: float | None = None, known: str | None = None,
           escalate: str | None = None) -> bool:
    """A co-tenant `by` sees `alias` `condition` (down or held), or no longer does (`cleared`). `known`: why
    the reporter knows the fault (its heal check's known_fault); "" says it does not, None keeps it.
    `escalate`: who the reporting heal check escalates to ("coordinator" takes the user's page).
    Returns whether the alias is a known machine."""
    now = time.time() if now is None else now
    if condition not in CONDITIONS or alias not in machines.load():
        return False
    key = f"{alias}:{condition}"
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        if cleared:
            if conds.pop(key, None) is None:
                return True
        else:
            c = conds.setdefault(key, {"alias": alias, "condition": condition, "first": now, "seen_by": []})
            c.update(last=now, text=_one_line(text) or c.get("text", ""),
                     grace_s=float(grace_s if grace_s is not None else c.get("grace_s", GRACE_S)))
            if by not in c["seen_by"]:
                c["seen_by"].append(by)
            if known is not None:
                kb = c.setdefault("known_by", {})
                if _one_line(known):
                    kb[by] = _one_line(known)
                else:
                    kb.pop(by, None)
            if escalate is not None:
                eb = c.setdefault("escalate_by", {})
                if escalate == "coordinator":
                    eb[by] = escalate
                else:
                    eb.pop(by, None)
        _write(ROUTES, doc)
    return True


def conditions() -> dict[str, dict]:
    return _read(ROUTES, {"conditions": {}}).get("conditions", {})


# known outages ------------------------------------------------------------------------------------

def set_outage(alias: str, why: str, by: str, expires: Any, owner: str = "", now: float | None = None) -> dict:
    """Record that `alias` is out for a known reason (a hardware repair) until `expires`: nobody is paged
    about it meanwhile. `owner`: who has the repair, often a human or an external ticket."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    why = _one_line(why)
    if not why:
        raise ValueError("say why the machine is out")
    if not expires:
        raise ValueError("give an end: --expires <delay or time>")
    rec = {"why": why, "owner": _one_line(owner, 120), "by": by, "since": now,
           "until": ends.parse_expires(expires, now)}
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        doc.setdefault("outages", {})[alias] = rec
        _write(ROUTES, doc)
    return rec


def clear_outage(alias: str) -> bool:
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        if (doc.get("outages") or {}).pop(alias, None) is None:
            return False
        _write(ROUTES, doc)
    return True


def outages(now: float | None = None, doc: dict | None = None) -> dict[str, dict]:
    """{alias: record} of the known outages not yet past their end."""
    now = time.time() if now is None else now
    got = (doc if doc is not None else _read(ROUTES, {"conditions": {}})).get("outages") or {}
    return {a: o for a, o in got.items() if isinstance(o, dict) and float(o.get("until") or 0) > now}


def outage_line(alias: str, o: dict, where=None) -> str:
    owner = f"; owner: {o['owner']}" if o.get("owner") else ""
    return f"{alias}: known outage until {ends.shown(o['until'], where)} (by {o.get('by') or '?'}{owner}): {o['why']}"


def on_machine(alias: str) -> Callable[[str], bool]:
    """Whether a resource name is on machine `alias` (names it, or its hostname)."""
    from . import responsibilities as rs
    rec = machines.load().get(alias) or {}
    needles = [alias] + ([str(rec["hostname"])] if rec.get("hostname") else [])
    return lambda res: any(rs._names(n, res) for n in needles)


def _demand(alias: str, reporters: list[str]) -> dict[str, list[str]]:
    """Read-only, from every project on this machine: the open tasks that use a resource on `alias`
    ("need"), and the pauses of resources on it ("paused", "user": set by the user). A reporter that
    cannot be read from here counts as needing it."""
    from . import globalcap, shared
    from . import responsibilities as rs
    from .coordinator import task_resources
    from .db import PAUSED_RESOURCES_KEY
    on = on_machine(alias)
    out: dict[str, list[str]] = {"need": [], "paused": [], "user": [], "shared": []}
    seen = set()
    for name, base in rs._projects(project.hostname()):
        seen.add(name)
        if not (base / "state" / "project.db").is_file():
            continue
        try:
            db = rs.ReadOnly(globalcap.connect_ro(base / "state" / "project.db"))
        except Exception:
            out["need"].append(f"{name} (its state cannot be read)")
            continue
        try:
            pz = db.kv(PAUSED_RESOURCES_KEY, {}) or {}
            pz = pz if isinstance(pz, dict) else {}
            for res, v in sorted(pz.items()):
                if on(res):
                    out["user" if isinstance(v, dict) and v.get("by") == "user" else "paused"].append(f"{name} {res}")
            for t in db.q("SELECT id, labels FROM tasks WHERE status NOT IN ('done','failed','cancelled')"):
                for res in sorted(task_resources(t)):
                    if on(res) and res not in pz and not shared.read_pause(res):
                        out["need"].append(f"{name} #{t['id']} ({res})")
        except Exception as e:
            out["need"].append(f"{name} (its state cannot be read: {type(e).__name__})")
        finally:
            db.close()
    for alias_res in sorted({r for r in rs._shared_resources(alias)}):
        sp = shared.read_pause(alias_res)
        if sp:
            out["user" if sp.get("by") == "user" else "shared"].append(f"shared {alias_res}")
    out["need"] += [f"{who} (not readable from here)" for who in reporters if who not in seen]
    return out


def quiet_reason(c: dict, now: float, doc: dict | None = None) -> tuple[str, bool]:
    """(why condition `c` escalates no further, whether nobody is paged at all); ("", False) when it pages as
    usual (module doc: Quiet outages)."""
    alias, cond = c["alias"], c["condition"]
    o = outages(now, doc).get(alias)
    if o:
        return (f"known outage until {ends.shown(o['until'])}"
                + (f", owned by {o['owner']}" if o.get("owner") else "") + f": {o['why']}"), True
    for ch in changes({alias}):
        if cond in expects_now(ch, now):
            return f"expected from machine change #{ch['id']} by {ch.get('by')}: {ch['what']}", True
    d = _demand(alias, list(c.get("seen_by") or []))
    if d["user"]:
        return f"the user paused {', '.join(d['user'])}", False
    if d["shared"]:
        return f"paused for every project: {', '.join(d['shared'])}", False
    known = c.get("known_by") or {}
    if known:
        return "known fault: " + "; ".join(f"{who}: {why}" for who, why in sorted(known.items())), False
    if not d["need"]:
        return "no open work uses it" + (f" (paused: {', '.join(d['paused'])})" if d["paused"] else ""), False
    return "", False


# who can act ---------------------------------------------------------------------------------------

def can_act(name: str, here: str | None = None) -> tuple[bool, str]:
    """Whether project `name` can take a page now, read-only from its own state: its daemon runs and
    ticks, it is not paused, and its coordinator is not logged out or failing. A project on another
    machine cannot be checked from here and counts as able; a name not in the registry cannot act."""
    from . import globalcap, overview
    here = here or project.hostname()
    entry = project.load_registry().get("projects", {}).get(name)
    if not isinstance(entry, dict):
        return False, "not a project of this user"
    if entry.get("host") and entry["host"] != here:
        return True, ""
    state = Path(entry.get("dir") or "") / project.FOLDER / "state"
    if not entry.get("dir") or not (state / "project.db").is_file():
        return False, "its state cannot be read"
    try:
        conn = globalcap.connect_ro(state / "project.db")
    except Exception:
        return False, "its state cannot be read"
    try:
        status, _ = overview._daemon(conn, state, here)
        if status != "running":
            return False, f"its daemon is {status}"
        paused = conn.execute("SELECT value FROM kv WHERE key='paused'").fetchone()
        if paused and paused[0] not in (None, "", "null", "false", "0"):
            return False, "it is paused"
        bad = conn.execute("SELECT key FROM alerts WHERE cleared IS NULL AND (key LIKE 'auth:%' OR key='coordinator') "
                           "LIMIT 1").fetchone()
        if bad:
            return False, "its coordinator is logged out" if bad[0].startswith("auth:") else "its coordinator is failing"
    except Exception as e:
        return False, f"its state cannot be read ({type(e).__name__})"
    finally:
        conn.close()
    return True, ""


def chain(alias: str, first: str | None = None) -> list[str]:
    m = machines.load().get(alias) or {}
    out = []
    for name in (first, m.get("recovery_owner"), m.get("recovery_fallback")):
        if name and name not in out:
            out.append(name)
    return out


def _step(page: dict, targets: list[str], now: float, able: Callable[[str], tuple[bool, str]]) -> str | None:
    """Advance one item's page state; returns the target to page now, or None."""
    if "stage" not in page:
        page.update(stage=0, at=now)
        return targets[0] if targets else USER
    stage = int(page["stage"])
    if stage >= len(targets):
        return None   # the user has it
    ok, why = able(targets[stage])
    if ok:
        page.pop("unable_since", None)
        page.pop("why", None)
    else:
        page.setdefault("unable_since", now)
        page["why"] = why
    if (not ok and now - page["unable_since"] >= UNAVAILABLE_S) or now - page["at"] >= REPAGE_S:
        page.update(stage=stage + 1, at=now)
        page.pop("unable_since", None)
        return targets[stage + 1] if stage + 1 < len(targets) else USER
    return None


def _pending(page: dict, target: str, kind: str, key: str, alias: str, episode: float, text: str) -> None:
    """Record the page due for this step; it replaces one an earlier step left undelivered."""
    page["pending"] = {"target": target, "kind": kind, "key": key, "alias": alias, "stage": int(page["stage"]),
                       "id": f"{key}@{int(episode)}#{int(page['stage'])}", "text": text}


def _vouched(c: dict, targets: list[str], now: float,
             healthy: Callable[[str, str, float, str], list[str]]) -> tuple[str, list[str]] | None:
    """(project, its checks): the first project in the page chain, not itself reporting condition `c`, whose
    own heal check of the machine that observes that condition ran healthy and fresh; None when none does."""
    for who in targets:
        if who in c.get("seen_by", []):
            continue
        checks = healthy(who, c["alias"], now, c["condition"])
        if checks:
            return who, checks
    return None


def route(now: float | None = None, able: Callable[[str], tuple[bool, str]] | None = None,
          claimer: str = "", healthy: Callable[[str, str, float, str], list[str]] | None = None,
          quiet: Callable[[dict, float, dict], tuple[str, bool]] | None = None) -> list[dict]:
    """The pages to deliver now, model-free: [{target, kind, key, alias, stage, id, text}]. Each due
    page is recorded as pending; the pending pages nobody holds (or whose claim lapsed after CLAIM_S)
    are claimed for `claimer` and returned. Mark each `delivered` once it went out, or `release` it.
    `quiet` (default quiet_reason) stops a condition's escalation, or every page of it."""
    now = time.time() if now is None else now
    able = able or can_act
    quiet = quiet or quiet_reason
    if healthy is None:
        from .responsibilities import healthy_checks as healthy
    out: list[dict] = []

    def claim(page: dict) -> None:
        pend = page.get("pending")
        if pend and now - float(pend.get("claimed") or 0) >= CLAIM_S:
            pend.update(claimed=now, claimed_by=claimer)
            out.append({k: v for k, v in pend.items() if k not in ("claimed", "claimed_by")})

    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        for key, c in list(conds.items()):
            if now - float(c.get("last") or 0) >= CONDITION_GONE_S:
                del conds[key]
                continue
            targets = chain(c["alias"])
            if not targets or now - float(c["first"]) < float(c.get("grace_s", GRACE_S)):
                continue
            page = c.setdefault("page", {})
            held = _vouched(c, targets, now, healthy)
            if held:
                # The owner's own check sees the machine healthy: no recovery page, one low note per episode.
                who, checks = held
                page["held"] = {"by": who, "checks": checks, "at": now}
                if (page.get("pending") or {}).get("kind") == "machine_condition":
                    continue   # a recovery page recorded before the hold stays unclaimed until it lifts
                nid = f"machine:{key}@{int(float(c['first']))}#held"
                why = ("likely the reporting project's own route problem" if c["condition"] == "down"
                       else "the reporting project's reading likely differs or is stale")
                if page.get("noted") != nid and not page.get("pending"):
                    page["noted"] = nid
                    page["pending"] = {
                        "target": who, "kind": "machine_condition_held", "key": f"machine:{key}",
                        "alias": c["alias"], "stage": int(page.get("stage") or 0), "id": nid, "severity": "low",
                        "text": f"Machine {c['alias']} reported {c['condition']} since {ends.shown(c['first'])} by "
                                f"{', '.join(c['seen_by'])}, but your own heal check {', '.join(checks)} sees it "
                                f"healthy: {why}. No recovery page "
                                f"was sent; nothing to do unless you see otherwise."}
                claim(page)
                continue
            page.pop("held", None)
            why, silent = quiet(c, now, doc)
            if why and (silent or "stage" in page):
                # Known, or nobody needs it: no further page, one daily-review record, the user's alert clears.
                page["quiet"] = why
                pend = page.get("pending")
                if pend and pend.get("kind") == "machine_condition" and (silent or int(pend.get("stage") or 0) > 0):
                    page.pop("pending")
                doc.setdefault("quiet", {})[c["alias"]] = {
                    "condition": c["condition"], "first": c["first"], "why": why, "at": now,
                    "seen_by": list(c.get("seen_by") or [])}
                claim(page)
                continue
            if page.pop("quiet", None) is not None and "stage" in page:
                page.update(at=now)   # the steps resume from now, not from the page before the quiet spell
                page.pop("unable_since", None)
            target = _step(page, targets, now, able)
            coordinators = [w for w in c.get("seen_by") or [] if (c.get("escalate_by") or {}).get(w) == "coordinator"]
            if target == USER and coordinators:
                target = coordinators[0]   # its heal check escalates to its coordinator, not the user
            if target:
                skipped = f" ({targets[page['stage'] - 1]} cannot act: {page.get('why') or 'no answer'})" \
                    if page["stage"] else ""
                # No running count in the text: a resend after a crash is the same note.
                _pending(page, target, "machine_condition", f"machine:{key}", c["alias"], float(c["first"]),
                         f"Machine {c['alias']} {c['condition']} since {ends.shown(c['first'])}, seen by "
                         f"{', '.join(c['seen_by'])}: {c.get('text') or 'no detail'}. "
                         f"Recovery page {page['stage'] + 1}{skipped}: "
                         f"check and recover it; record any live change with `ttp machines change add`.")
            claim(page)
        doc["quiet"] = {a: q for a, q in (doc.get("quiet") or {}).items()
                        if isinstance(q, dict) and now - float(q.get("at") or 0) < QUIET_KEPT_S}
        doc["outages"] = outages(now, doc)
        _write(ROUTES, doc)
        ledger = _read(LEDGER, {"next": 1, "changes": []})
        changed = False
        for c in ledger.get("changes", []):
            late = overdue(c, now)
            if not late:
                continue
            page = c.setdefault("page", {})
            target = _step(page, chain(c["alias"], c.get("by")), now, able)
            changed = True
            if target:
                _pending(page, target, "machine_change_overdue", f"machine-change:{c['id']}", c["alias"],
                         float(c.get("page_from") or c["added"]),
                         f"Machine change #{c['id']} on {c['alias']} is overdue ({late}): {c['what']} "
                         f"(by {c.get('by')}). Page {page['stage'] + 1}: undo it with `{c['undo']}`, "
                         f"then `ttp machines change close {c['id']}`.")
            claim(page)
        if changed:
            _write(LEDGER, ledger)
    return out


def _settle(sent: dict, done: bool) -> bool:
    """Mark the pending page `sent` delivered (done) or free to retry; False when it is no longer pending."""
    with _locked():
        for name, empty in ((ROUTES, {"conditions": {}}), (LEDGER, {"next": 1, "changes": []})):
            doc = _read(name, empty)
            items = doc.get("conditions", {}).values() if name == ROUTES else doc.get("changes", [])
            for c in items:
                pend = (c.get("page") or {}).get("pending") if isinstance(c, dict) else None
                if not pend or pend.get("id") != sent.get("id"):
                    continue
                if done:
                    c["page"].pop("pending")
                    c["page"]["delivered"] = sent["id"]
                else:
                    pend.pop("claimed", None)
                    pend.pop("claimed_by", None)
                _write(name, doc)
                return True
    return False


def delivered(page: dict) -> bool:
    """Page `page` (from `route`) went out: it is not sent again."""
    return _settle(page, True)


def release(page: dict) -> bool:
    """Page `page` (from `route`) could not go out: the next tick retries it."""
    return _settle(page, False)


def condition_open(key: str, now: float | None = None) -> bool:
    """Whether condition `key` (alias:condition) is still reported (alerts.holds)."""
    now = time.time() if now is None else now
    c = conditions().get(key)
    return isinstance(c, dict) and now - float(c.get("last") or 0) < CONDITION_GONE_S \
        and not (c.get("page") or {}).get("quiet")


def change_overdue(cid: int, now: float | None = None) -> bool:
    """Whether change #cid is open and overdue (alerts.holds)."""
    return any(c.get("id") == cid and overdue(c, now) for c in changes())


# probes -------------------------------------------------------------------------------------------

class Probes:
    """Runs the until-probes of the entries project `p` made, in the background, from its root."""

    def __init__(self, p, log: Callable[[str], None] = print):
        self.p, self.log = p, log
        self._procs: dict[int, tuple[subprocess.Popen, float, str]] = {}
        self._probed: dict[int, float] = {}

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        passed = []
        for cid, (proc, started, probe) in list(self._procs.items()):
            rc = proc.poll()
            if rc is None and now - started < ends.PROBE_TIMEOUT_S:
                continue
            del self._procs[cid]
            if rc is None:
                ends._kill(proc)
            elif rc == 0:
                passed.append((cid, probe))
        if passed:
            with _locked():
                doc = _read(LEDGER, {"next": 1, "changes": []})
                for c in doc.get("changes", []):
                    if (c.get("id"), c.get("probe")) in passed and not c.get("closed"):   # not a probe an edit replaced
                        c["probe_passed"] = now
                _write(LEDGER, doc)
        for c in changes():
            if c.get("kind") == POWER_CYCLE and float(c.get("expires") or 0) <= now:   # any daemon: the maker's may be gone
                with contextlib.suppress(ValueError):   # another daemon closed it first
                    close_change(int(c["id"]), "the planned power cycle is past", self.p.name, now)
        for c in changes():
            cid = c.get("id")
            if c.get("by") != self.p.name or not c.get("probe") or c.get("probe_passed") or cid in self._procs \
                    or now - self._probed.get(cid, 0) < ends.PROBE_EVERY_S:
                continue
            self._probed[cid] = now
            try:
                self._procs[cid] = (subprocess.Popen(c["probe"], shell=True, cwd=str(self.p.root),
                                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, start_new_session=True), now, c["probe"])
            except OSError as e:
                self.log(f"machine change #{cid}: until-probe could not start: {e}")

    def stop(self) -> None:
        for proc, *_ in self._procs.values():
            ends._kill(proc)
        self._procs.clear()


# digest -------------------------------------------------------------------------------------------

def resource_machines(p) -> list[str]:
    """The machines in the user's list that the charter's Resources section names."""
    from .prompts import charter_sections
    try:
        text = "\n".join("\n".join(body) for head, body in charter_sections(p.charter_path.read_text())
                         if ends.section_name(head).lower().startswith("resources"))
    except OSError:
        return []
    return sorted(a for a in machines.load() if re.search(rf"(?<![\w.-]){re.escape(a)}(?![\w-]|\.\w)", text))


def digest_lines(p, now: float | None = None) -> list[str]:
    """The open changes and conditions on the machines in `p`'s Resources, one line each, and each
    machine there without a recovery owner, named once per project."""
    now = time.time() if now is None else now
    mine = resource_machines(p)
    if not mine:
        return []
    known = machines.load()
    lines = [change_line(c, now, p) for c in changes(set(mine))]
    for c in conditions().values():
        if c.get("alias") in mine:
            owner = (known.get(c["alias"]) or {}).get("recovery_owner")
            held = (c.get("page") or {}).get("held")
            hush = (c.get("page") or {}).get("quiet")
            lines.append(f"{c['alias']} {c['condition']} since {ends.shown(c['first'], p)} (seen by "
                         f"{', '.join(c.get('seen_by', []))}; recovery owner: {owner or 'none set'}"
                         + (f"; page held: {held['by']}'s heal check sees it healthy" if held else "")
                         + (f"; not escalated: {hush}" if hush else "") + ")")
    lines += [outage_line(a, o, p) for a, o in sorted(outages(now).items()) if a in mine]
    named = list(p.db.kv(GAP_KV) or [])
    gaps = [a for a in mine if not (known.get(a) or {}).get("recovery_owner") and a not in named]
    if gaps:
        lines.append("No recovery owner set for " + ", ".join(gaps) + ": a long down or held condition there "
                     "reaches nobody. Set one: `ttp machines set <alias> --owner <project> [--fallback <project>]` "
                     "(named once).")
        p.db.set_kv(GAP_KV, named + gaps)
    return lines


def review_lines(p, now: float | None = None) -> list[str]:
    """For the daily review: each outage of the last day on a machine in `p`'s Resources (or one it is the
    recovery owner or fallback of) that was not paged, and why; one line per machine."""
    now = time.time() if now is None else now
    known = machines.load()
    mine = set(resource_machines(p)) | {a for a, m in known.items()
                                        if p.name in (m.get("recovery_owner"), m.get("recovery_fallback"))}
    got = _read(ROUTES, {"conditions": {}}).get("quiet") or {}
    return [f"{a} {q.get('condition')} since {ends.shown(float(q.get('first') or 0), p)} (seen by "
            f"{', '.join(q.get('seen_by') or []) or '?'}), last {ends.shown(float(q['at']), p)}: not paged: {q.get('why')}"
            for a, q in sorted(got.items()) if a in mine and isinstance(q, dict)
            and now - float(q.get("at") or 0) < REVIEW_S]
