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
recorded with `ttp machines outage <alias> "<why>" [--owner <who>] [--covers <parts>] --expires <t>` (a
hardware repair, often owned by a human or an external ticket; `covers`, optional, lists the parts it explains
from OUTAGE_COVERS, or services on the machine by name, for co-tenant watchers and the harness's own heal checks,
which it then quiets only for results it explains (heal.outage_explains)), or an open change that expects the condition (`--expect down`).
The owner's first page still goes, but nothing escalates to the fallback or the user, while: the user paused
a resource on the machine, a reporting heal check carries a `known_fault`, or no open task of any project on
this machine uses it (a task's resource on it that its project paused does not count). A reporter this
machine cannot read counts as needing it. Each such outage is recorded once per machine (`quiet`), for the
daily review (review_lines), never alerted; its user alert clears. Once the reason ends, the steps resume
from that moment.

Change ledger. Every live change to a shared machine (a drop-in, a disabled recovery switch, a pause) is
recorded with `ttp machines change add <alias> "<what>" --undo "<cmd>" (--expires <t> | --until-probe
"<cmd>" | --until-task <id>)`: who made it (the project), why (the text), how to undo it and when it ends. Each co-tenant's
digest lists the open entries for machines in its Resources, one line each. An entry is overdue when it
expires, its until-probe passes (exit 0: what it waited for is over, so the undo is due) or its until-task
(a task of the project that made it, such as the follow-up that uses a staged build tree) is done, failed,
cancelled or gone; a probe-only or task-only entry also expires after PROBE_END_S, and a continuation task
re-extends it with `ttp machines change edit <id> --until-task <its own id>`. The daily review lists the
project's open entries whose task is over (task_lines). An overdue entry pages the project that made it, then the machine's
owner, then its fallback, each after the one before cannot act for UNAVAILABLE_S or let REPAGE_S pass,
then the user. Nothing is undone automatically: whoever is paged runs the undo and closes the entry
(`ttp machines change close <id>`). Probes run only in the daemon of the project that made the entry,
from its root, as end probes do; a broken one (an exit code other than 0, 1, 75 or ssh's own 255, or a
timeout) queues one low `change_probe_broken` event per kind of breakage; the same daemon reads its until-tasks'
states. `ttp machines change edit <id> [--what] [--undo] [--expires | --until-probe | --until-task]`
corrects an open entry in place (same id), with an edit trail in the entry.

Planned power cycles. `ttp machines change add <alias> "<why>" --power-cycle-at <t>` records a power-cycle
intent: an entry of kind `power_cycle` that needs no undo and closes itself POWER_CYCLE_AFTER_S after T
(never overdue; any co-tenant daemon closes it once past). It expects the machine `down` only from
POWER_CYCLE_LEAD_S before T until it ends (expects_now), so it never hides an outage outside the cycle. From POWER_CYCLE_LEAD_S before T until as long after it, each
daemon tells its running workers on that machine (the daemon's own host, or a task resource on it) once,
through their steer.md, to commit and hand off now (daemon.power_cycle_notices). An intent `ttp drain
--reboot` writes carries `hold_from`, when it was written: its window starts there when that is earlier
(cycle_window), so the drain is held and steered from its start (hostcycle); its expects still start
POWER_CYCLE_LEAD_S before T, which the drain moves to the cycle once it records it.

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
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import detach, ends, machines, outagetask, project
from .db import TERMINAL_TASK_STATES

LEDGER = "machine-changes.json"
ROUTES = "machine-routes.json"
LOCK = "machine-ledger.lock"
CLAIM_S = 120               # a pending page one daemon took and did not mark delivered is retried after this
UNAVAILABLE_S = 1800         # a target that cannot act this long is passed over
REPAGE_S = 86400             # an item still open this long after its page is paged again, one step on
GRACE_S = 1800               # default: how long a condition lasts before it is routed
CONDITION_GONE_S = 3 * 3600  # a condition nobody reported this long is over
PROBE_END_S = 7 * 86400      # an entry with only an until-probe or until-task is overdue after this anyway
CLOSED_KEPT_S = 30 * 86400   # closed entries kept this long
TEXT_CHARS = 300
CONDITIONS = ("down", "held")
# what a known outage explains (`ttp machines outage --covers`), besides a service's name; none given:
# the whole machine
OUTAGE_COVERS = ("host", "power", "network", "tray", "chip", "fabric")
COVER_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")   # a service a known outage names
QUIET_KEPT_S = 2 * 86400     # a quiet outage's daily-review record is kept this long after it was last seen
REVIEW_S = 86400             # the daily review lists the quiet outages seen within this
GAP_KV = "machine_owner_gap_named"
POWER_CYCLE = "power_cycle"
POWER_CYCLE_LEAD_S = 600     # running workers on the machine are told to hand off this long before the cycle
POWER_CYCLE_AFTER_S = 3600   # a power-cycle intent closes itself this long after its time
USER = "the user"
# The coordinator digest shows each change on one line of about DIGEST_LINE_CHARS (change_line's clip): a
# long reason, undo or probe would otherwise fill the digest, which every turn writes to the cache again.
DIGEST_LINE_CHARS = 300
CLIP_PART_CHARS = 80   # an undo, until-probe or overdue cause in a clipped line
CLIP_WHAT_MIN = 60     # the reason keeps at least this much


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
               task: int | None = None, now: float | None = None, expects=None, extra: dict | None = None,
               until_task: int | None = None) -> dict:
    """Record a live change to machine `alias` made by project `by`; returns the entry. `expects`: the
    event fingerprints or machine conditions (down, held) the change is known to cause, which then
    keep the coordinator's turn routine (coordinator._known_case). `extra`: more fields (a kind).
    `until_task`: a task of project `by` whose end (done, failed, cancelled, gone) makes it overdue
    (check_task validates it)."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    what, undo, probe = _one_line(what), _one_line(undo, 1000), _one_line(until_probe, ends.PROBE_CHARS)
    if not what or not undo:
        raise ValueError("say what changed and give the --undo command")
    if probe and until_task:
        raise ValueError("give one end: --until-probe or --until-task")
    if not expires and not probe and not until_task:
        raise ValueError(END_HINT)
    if probe:
        ends.check_sh(probe, "--until-probe")
    end = float(expires) if isinstance(expires, (int, float)) else \
        ends.parse_expires(expires, now) if expires else now + PROBE_END_S   # a number: an epoch time
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        n = int(doc.get("next") or 1)
        entry = {"id": n, "alias": alias, "what": what, "undo": undo, "by": by, "task": task, "added": now,
                 "expires": end, "probe": probe or None, **({"until_task": int(until_task)} if until_task else {}),
                 **(extra or {})}
        if expects:
            entry["expects"] = [_one_line(x, 200) for x in expects if _one_line(x, 200)]
        doc["next"] = n + 1
        doc["changes"] = [c for c in doc.get("changes", []) if not c.get("closed")
                          or now - float(c["closed"]) < CLOSED_KEPT_S] + [entry]
        _write(LEDGER, doc)
    return entry


def add_power_cycle(alias: str, at: Any, by: str, why: str = "", task: int | None = None,
                    now: float | None = None, extra: dict | None = None) -> dict:
    """Record a planned power cycle of `alias` at `at` (a delay or a time, as --expires takes).
    `extra`: more fields (`ttp drain --reboot` adds the boot it was made on and its reason)."""
    now = time.time() if now is None else now
    when = ends.parse_expires(at, now, what="--power-cycle-at")
    what = f"power-cycle intent, at {ends.shown(when)}" + (f": {_one_line(why)}" if _one_line(why) else "")
    return add_change(alias, what, "none: a planned power cycle; the entry closes itself after it", by,
                      expires=when + POWER_CYCLE_AFTER_S, task=task, now=now, expects=["down"],
                      extra={**(extra or {}), "kind": POWER_CYCLE, "at": when})


def cycle_window(c: dict) -> tuple[float, float]:
    """(start, end) of power-cycle intent c's window: POWER_CYCLE_LEAD_S either side of its time, starting
    at its `hold_from` instead when that is earlier (a drain's intent holds from when it was written)."""
    at = float(c.get("at") or 0)
    start = at - POWER_CYCLE_LEAD_S
    if isinstance(c.get("hold_from"), (int, float)):
        start = min(start, float(c["hold_from"]))
    return start, at + POWER_CYCLE_LEAD_S


def power_cycles_due(now: float | None = None) -> list[dict]:
    """Open power-cycle intents whose window (cycle_window) holds now."""
    now = time.time() if now is None else now
    return [c for c in changes() if c.get("kind") == POWER_CYCLE
            and cycle_window(c)[0] <= now <= cycle_window(c)[1]]


def retime_power_cycle(cid: int, at: float, by: str, now: float | None = None,
                       hold_from: float | None = None, drain: float | None = None) -> dict:
    """Move open power-cycle intent #cid to epoch time `at` (its end follows), with an edit trail: a drain
    that runs longer pushes it on, and the cycle itself, once recorded, brings it to now. `hold_from`: its
    window also starts no later than that (and no later than it did), in the same write, so a drain that
    takes up an intent and pushes it on keeps holding from now. `drain`: the start time of the drain that
    moves it, kept in the edit so its edits are told from another drain's of the same project."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid
                  and c.get("kind") == POWER_CYCLE and not c.get("closed")), None)
        if c is None:
            raise ValueError(f"no open power-cycle intent #{cid}")
        what = str(c.get("what") or "")
        new = {"at": at, "expires": at + POWER_CYCLE_AFTER_S,
               "what": f"power-cycle intent, at {ends.shown(at)}" + (f": {what.split(': ', 1)[1]}" if ": " in what else "")}
        if hold_from is not None:
            new["hold_from"] = min(cycle_window(c)[0], float(hold_from))
        c.setdefault("edits", []).append({"at": now, "by": by, "was": {k: c.get(k) for k in new}}
                                         | ({"drain": drain} if drain is not None else {}))
        c.update(new)
        _write(LEDGER, doc)
        return c


# The fields a retime moves (retime_power_cycle) and a restore puts back (restore_power_cycle).
RETIMED = ("at", "expires", "what", "hold_from")


def restore_power_cycle(cid: int, was: dict, by: str, mark: str, now: float | None = None,
                        edits: int | None = None) -> dict:
    """Put open power-cycle intent #cid's time fields (RETIMED) back to `was` (a None or missing field is
    removed), with an edit trail entry that carries `mark` as `undo`; once per mark: a second call with the
    same mark changes nothing. `edits`: the length of the edit trail `was` was worked out from; when the
    trail has grown since, nothing changes either (the caller works it out again)."""
    now = time.time() if now is None else now
    with _locked():
        doc = _read(LEDGER, {"next": 1, "changes": []})
        c = next((c for c in doc.get("changes", []) if isinstance(c, dict) and c.get("id") == cid
                  and c.get("kind") == POWER_CYCLE and not c.get("closed")), None)
        if c is None:
            raise ValueError(f"no open power-cycle intent #{cid}")
        if any(e.get("undo") == mark for e in c.get("edits") or []) \
                or edits is not None and len(c.get("edits") or []) != edits:
            return c
        c.setdefault("edits", []).append({"at": now, "by": by, "was": {k: c.get(k) for k in RETIMED}, "undo": mark})
        for k in RETIMED:
            if was.get(k) is None:
                c.pop(k, None)
            else:
                c[k] = was[k]
        _write(LEDGER, doc)
        return c


END_HINT = "give an end: --expires <delay or time>, --until-probe <command> or --until-task <id>"


def check_task(p, tid: int) -> None:
    """Refuse an until-task that is not an open task of project `p` (the entry's author)."""
    row = p.db.task(int(tid))
    if row is None:
        raise ValueError(f"no task #{tid} in project {p.name}: --until-task names one of its open tasks")
    if row["status"] in TERMINAL_TASK_STATES:
        raise ValueError(f"task #{tid} is {row['status']} already: give an open task, or another end")


def task_over(p, tid: Any) -> str:
    """Why until-task `tid` of project `p` is over ("" while it is open): done, failed, cancelled or gone."""
    try:
        row = p.db.task(int(tid))
    except (TypeError, ValueError):
        return f"its until-task #{tid} is not a task id"
    if row is None:
        return f"its until-task #{tid} is gone"
    return f"its until-task #{tid} is {row['status']}" if row["status"] in TERMINAL_TASK_STATES else ""


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
                until_probe: str | None = None, now: float | None = None, until_task: int | None = None) -> dict:
    """Correct open entry #cid in place: its text, undo command or end (--expires, --until-probe or
    --until-task, as add_change takes them; the same until-task again restarts its PROBE_END_S, so a
    continuation task re-extends the entry). Keeps its id, alias, author and creation time; records who edited it, when
    and the values it replaced in `edits`. A new end starts afresh (`page_from`): its until-probe result and overdue
    pages are dropped. Refuses a closed or unknown id, a power-cycle intent's undo or end, and an edit
    that changes nothing."""
    now = time.time() if now is None else now
    if sum(map(bool, (expires, until_probe, until_task))) > 1:
        raise ValueError("give one end: --expires, --until-probe or --until-task")
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
        new.update(probe=probe, until_task=None, expires=now + PROBE_END_S)
    elif until_task:
        new.update(probe=None, until_task=int(until_task), expires=now + PROBE_END_S)
    elif expires:
        new.update(probe=None, until_task=None, expires=float(expires) if isinstance(expires, (int, float))
                   else ends.parse_expires(expires, now))
    if not new:
        raise ValueError("say what to change: --what, --undo, --expires, --until-probe or --until-task")
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
            for k in ("probe_passed", "probe_job_failed", "task_ended", "page"):
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
    if c.get("probe_job_failed"):
        return f"the detached job its until-probe waits on ended but failed ({c['probe_job_failed'] if isinstance(c['probe_job_failed'], str) else 'probe exit ' + str(c['probe_job_failed'])})"
    if c.get("probe_passed"):
        return "its until-probe passed"
    if c.get("task_ended"):
        return str(c.get("task_end_why") or f"its until-task #{c.get('until_task')} ended")
    if float(c.get("expires") or 0) <= now:
        return f"expired {ends.shown(float(c['expires']), where)}"
    return ""


def expects_now(c: dict, now: float | None = None) -> list[str]:
    """What entry `c` expects at `now`: nothing once it is closed or overdue, and for a power-cycle intent
    nothing outside POWER_CYCLE_LEAD_S before its time until its end, so it never quiets an outage
    days before the cycle, or after it when nobody closed it. A drain's `hold_from` does not widen this:
    the drain moves the time to the cycle once it records it (hostcycle.cycle_now)."""
    now = time.time() if now is None else now
    if c.get("closed") or overdue(c, now):
        return []
    if c.get("kind") == POWER_CYCLE:
        at = float(c.get("at") or 0)
        if not at - POWER_CYCLE_LEAD_S <= now < float(c.get("expires") or at + POWER_CYCLE_AFTER_S):
            return []
    return [str(x) for x in c.get("expects") or []]


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:max(n - 1, 0)].rstrip(" ,;:") + "…"


def change_line(c: dict, now: float | None = None, where=None, clip: int | None = None) -> str:
    """Entry `c` on one line. `clip`: at most about that many characters (the coordinator digest): the
    reason is cut first, then a long undo, probe or overdue cause, so the id, alias, author, the start of
    the reason, the undo and the end always show."""
    late = overdue(c, now, where)
    part = (lambda t: _clip(t, CLIP_PART_CHARS)) if clip else str
    end = f"until-probe `{part(c['probe'])}`, at the latest {ends.shown(c['expires'], where)}" if c.get("probe") \
        else f"until task #{c['until_task']} ends, at the latest {ends.shown(c['expires'], where)}" \
        if c.get("until_task") else f"ends {ends.shown(c['expires'], where)}"
    by = c.get("by", "?") + (f" #{c['task']}" if c.get("task") else "")
    end += f"; expects {', '.join(c['expects'])}" if c.get("expects") else ""
    head, tail = f"#{c['id']} {c['alias']}: ", (f" (by {by}; undo: `{part(c['undo'])}`; {end})"
                                                + (f" OVERDUE: {part(late)}" if late else ""))
    what = _clip(c["what"], max(clip - len(head) - len(tail), CLIP_WHAT_MIN)) if clip else c["what"]
    return head + what + tail


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

def set_outage(alias: str, why: str, by: str, expires: Any, owner: str = "", now: float | None = None,
               covers: Any = None) -> dict:
    """Record that `alias` is out for a known reason (a hardware repair) until `expires`: nobody is paged
    about it meanwhile. `owner`: who has the repair, often a human or an external ticket. `covers`: the
    parts it explains (OUTAGE_COVERS, or a service by name), stored for co-tenant watchers and heal
    checks; none: the whole machine."""
    now = time.time() if now is None else now
    if alias not in machines.load():
        raise ValueError(f"no machine {alias!r} in `ttp machines list`")
    why = _one_line(why)
    if not why:
        raise ValueError("say why the machine is out")
    if not expires:
        raise ValueError("give an end: --expires <delay or time>")
    parts = outage_covers(covers)
    rec = {"why": why, "owner": _one_line(owner, 120), "by": by, "since": now,
           "until": ends.parse_expires(expires, now)}
    if parts:
        rec["covers"] = parts
    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        doc.setdefault("outages", {})[alias] = rec
        _write(ROUTES, doc)
    return rec


def outage_covers(covers: Any) -> list[str]:
    """`covers` (a list, or comma-separated words): the OUTAGE_COVERS in that order, then service names."""
    if isinstance(covers, str):
        covers = [covers]
    words = {w.strip().lower() for c in (covers or []) for w in str(c).split(",") if w.strip()}
    bad = sorted(w for w in words - set(OUTAGE_COVERS) if not COVER_NAME.fullmatch(w))
    if bad:
        raise ValueError(f"--covers takes {', '.join(OUTAGE_COVERS)} or a service's name (one word); "
                         f"not {', '.join(bad)}")
    return [c for c in OUTAGE_COVERS if c in words] + sorted(words - set(OUTAGE_COVERS))


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
    owner += f"; covers: {', '.join(o['covers'])}" if o.get("covers") else ""
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


def _ask(able: Callable[[str], tuple[bool, str]], who: str) -> tuple[bool, str]:
    """able(who); any answer that is not an (ok, why) pair counts as cannot act, as can_act answers for a
    name it does not know."""
    got = able(who)
    if isinstance(got, (tuple, list)) and len(got) == 2:
        return bool(got[0]), str(got[1] or "")
    return False, "not a project of this user"


def _step(page: dict, targets: list[str], now: float, able: Callable[[str], tuple[bool, str]]) -> str | None:
    """Advance one item's page state; returns the target to page now, or None."""
    if "stage" not in page:
        page.update(stage=0, at=now)
        return targets[0] if targets else USER
    stage = int(page["stage"])
    if stage >= len(targets):
        return None   # the user has it
    ok, why = _ask(able, targets[stage])
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


def _task_host(c: dict, targets: list[str], able: Callable[[str], tuple[bool, str]]) -> str:
    """The project an outage task goes to: the fallback, else the first detecting project, that can act;
    else the fallback (or the first detector, else the owner) anyway."""
    owner = targets[0]
    rest = [t for t in targets[1:] if t != owner]
    seen = [s for s in c.get("seen_by") or [] if s != owner and s not in rest]
    for who in rest + seen:
        if _ask(able, who)[0]:
            return who
    return (rest + seen + [owner])[0]


def _task_step(c: dict, key: str, targets: list[str], page: dict, now: float,
               able: Callable[[str], tuple[bool, str]], b: dict, acts: list[dict],
               status: Callable[[str, str], str | None]) -> None:
    """Advance condition `c`'s outage task (outagetask.py): queue it once its owner cannot act, or did not
    record a recovery action within b['owner_s']; then, past its due time while open, one high page to its
    project, and past b['escalate_s'] more, one normal-severity user page (straight away when no project
    can act; to the reporter's coordinator when its heal check escalates there). A creating page still
    pending is never replaced."""
    task, owner = c.get("task"), targets[0]
    if task is None:
        ok, why = _ask(able, owner)
        if ok:
            if now - float(c["first"]) < b["owner_s"] or any(
                    a.get("alias") == c["alias"] and float(a.get("added") or 0) >= float(c["first"]) for a in acts):
                return
            why = (f"recovery owner {owner} recorded no recovery action (`ttp machines change add`) within "
                   f"{b['owner_s'] / 60:g} min of the outage's start")
        else:
            why = f"recovery owner {owner} cannot act: {why}"
        host, due = _task_host(c, targets, able), now + b["due_s"]
        paged = targets[:int(page.get("stage") or 0) + 1] if "stage" in page else []
        tried = (f"recovery page(s) to {', '.join(paged)}" if paged else "no recovery page yet") + \
            (f"; {page['why']}" if page.get("why") else "")
        rec = outagetask.record(c, key, why, tried, due)
        c["task"] = {"project": host, "at": now, "due": due, "label": rec["label"], "stage": 0,
                     "pending": {"target": host, "kind": outagetask.KIND, "key": f"machine:{key}",
                                 "alias": c["alias"], "stage": 0, "id": rec["label"], "outage": rec,
                                 "text": f"Machine {c['alias']} {c['condition']} since {ends.shown(c['first'])}: "
                                         f"{why}. A tracked outage task ({rec['label']}) is queued in {host}, "
                                         f"due {ends.shown(due)}."}}
        return
    stage, due = int(task.get("stage") or 0), float(task["due"])
    pend = task.get("pending")
    if stage >= 2 or now < due or (pend and pend.get("kind") == outagetask.KIND):
        return
    if outagetask.closed(status(task["project"], task["label"])):
        return
    late = f"Outage task {task['label']} in {task['project']} for machine {c['alias']} {c['condition']} " \
           f"(since {ends.shown(c['first'])}) is open past its due time {ends.shown(due)}"
    if stage == 0:
        target = next((w for w in [task["project"]] + list(c.get("seen_by") or []) if _ask(able, w)[0]), None)
        if target:
            task["stage"] = 1
            task["pending"] = {"target": target, "kind": outagetask.OVERDUE_KIND, "key": f"machine:{key}",
                               "alias": c["alias"], "stage": 1, "id": f"{task['label']}#overdue",
                               "severity": "high",
                               "text": f"{late}: check it and recover the machine now, or say who must act."}
            return
        tail = "; no project can act on it"   # nobody to wake: straight to the user, once
    elif now < due + b["escalate_s"]:
        return
    else:
        tail = f" by more than {b['escalate_s'] / 60:g} min"
    coordinators = [w for w in c.get("seen_by") or [] if (c.get("escalate_by") or {}).get(w) == "coordinator"]
    task["stage"] = 2
    task["pending"] = {"target": coordinators[0] if coordinators else USER, "kind": outagetask.OVERDUE_KIND, "key": f"machine:{key}",
                       "alias": c["alias"], "stage": 2, "id": f"{task['label']}#user", "severity": "normal",
                       "text": f"{late}{tail}."}

def route(now: float | None = None, able: Callable[[str], tuple[bool, str]] | None = None,
          claimer: str = "", healthy: Callable[[str, str, float, str], list[str]] | None = None,
          quiet: Callable[[dict, float, dict], tuple[str, bool]] | None = None,
          budgets: dict | None = None, task_status: Callable[[str, str], str | None] | None = None) -> list[dict]:
    """The pages to deliver now, model-free: [{target, kind, key, alias, stage, id, text}]. Each due
    page is recorded as pending; the pending pages nobody holds (or whose claim lapsed after CLAIM_S)
    are claimed for `claimer` and returned. Mark each `delivered` once it went out, or `release` it.
    `quiet` (default quiet_reason) stops a condition's escalation, or every page of it. `budgets`
    (outagetask.budgets) times the outage task; `task_status` reads its state (outagetask.task_status)."""
    now = time.time() if now is None else now
    able = able or can_act
    quiet = quiet or quiet_reason
    b = budgets or outagetask.budgets(None)
    task_status = task_status or outagetask.task_status
    if healthy is None:
        from .responsibilities import healthy_checks as healthy
    out: list[dict] = []

    def claim(page: dict) -> None:
        pend = page.get("pending")
        if pend and now - float(pend.get("claimed") or 0) >= CLAIM_S:
            pend.update(claimed=now, claimed_by=claimer)
            out.append({k: v for k, v in pend.items() if k not in ("claimed", "claimed_by")})

    def settle_task(c: dict) -> None:
        # A held or quiet condition escalates no further; a queued task still reaches its project.
        task = c.get("task") or {}
        if (task.get("pending") or {}).get("kind") == outagetask.OVERDUE_KIND:
            task.pop("pending")
        claim(task)

    with _locked():
        doc = _read(ROUTES, {"conditions": {}})
        conds = doc.setdefault("conditions", {})
        acts = _read(LEDGER, {"next": 1, "changes": []}).get("changes", [])
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
                settle_task(c)
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
                settle_task(c)
                continue
            if page.pop("quiet", None) is not None and "stage" in page:
                page.update(at=now)   # the steps resume from now, not from the page before the quiet spell
                page.pop("unable_since", None)
            target = _step(page, targets, now, able)
            coordinators = [w for w in c.get("seen_by") or [] if (c.get("escalate_by") or {}).get(w) == "coordinator"]
            if target == USER and coordinators:
                target = coordinators[0]   # its heal check escalates to its coordinator, not the user
            if target:
                # The text is fixed for this step (its duration too): a resend after a crash is the same note.
                _pending(page, target, "machine_condition", f"machine:{key}", c["alias"], float(c["first"]),
                         condition_text(c, page, targets, target, now))
            claim(page)
            if why:
                settle_task(c)   # quiet (paused, a known fault, no work needs it): its owner hears once, no task
                continue
            _task_step(c, key, targets, page, now, able, b, acts, task_status)
            claim(c.get("task") or {})
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


def acting_now(c: dict) -> str:
    """Who is acting on condition `c` now, from its page state (outagemsg wording)."""
    from . import outagemsg
    page = c.get("page") or {}
    targets = chain(c["alias"])
    if not targets:
        return "nobody: no recovery owner set"
    if page.get("held"):
        return f"nobody: {page['held'].get('by')}'s heal check sees it healthy"
    if page.get("quiet"):
        return f"nobody: not escalated ({page['quiet']})"
    if "stage" not in page:
        return f"nobody yet: {targets[0]} is paged once its grace ends"
    stage = int(page["stage"])
    if stage >= len(targets):
        return outagemsg.TO_USER
    m = machines.load().get(c["alias"]) or {}
    who = targets[stage]
    return outagemsg.actor(who, "owner" if who == m.get("recovery_owner") else
                           "fallback" if who == m.get("recovery_fallback") else "",
                           # `why` outlives a step (the next page names it); `unable_since` is this target's
                           f"cannot act yet: {page['why']}" if page.get("why") and page.get("unable_since") else "")


def condition_line(c: dict, now: float, where=None, so_far: bool = True) -> str:
    """'down since <start> (<duration> so far; seen by ...): <text>. Acting now: <who>[. Tracked task: #N]'.
    `so_far` False leaves out the duration (the coordinator digest, which would else reword it each turn)."""
    from . import outagemsg
    page = c.get("page") or {}
    lasted = f"{outagemsg.duration(now - float(c['first']))} so far; " if so_far else ""
    task = tracked(c)[0]
    tid = outagemsg._int(task)
    name = f"#{tid}" if tid else task if isinstance(task, str) else ""
    return (f"{c['condition']} since {ends.shown(c['first'], where)} ({lasted}seen by "
            f"{', '.join(c.get('seen_by') or [])}): {c.get('text') or 'no detail'}. "
            f"{outagemsg.ACTING} {acting_now(c)}" + (f". Tracked task: {name}" if name else ""))


def tracked(c: dict) -> tuple[Any, Any]:
    """(task, due) a condition's messages name: its outage task (outagetask.py) as '<label> in <project>',
    else a task id and due time a page or the condition records."""
    t, page = c.get("task"), c.get("page") or {}
    if isinstance(t, dict):
        return f"{t.get('label')} in {t.get('project')}", t.get("due")
    return page.get("task") or t, page.get("due") or c.get("due")


def condition_text(c: dict, page: dict, targets: list[str], target: str, now: float) -> str:
    """A machine_condition page's text for this step (outagemsg.facts): since when and how long, who is
    acting now (the target, its role, and who was passed over and why), the tracked task and due time
    when the page or condition records one, the pages already sent, and the next step."""
    from . import outagemsg
    stage = int(page.get("stage") or 0)
    m = machines.load().get(c["alias"]) or {}
    passed = f"{targets[stage - 1]} cannot act: {page.get('why') or 'no answer'}" if 0 < stage <= len(targets) else ""
    if target == USER:
        acting = outagemsg.actor(None, passed=passed)
        step = (f"the user checks and recovers {c['alias']}, or names a project that can "
                f"(`ttp machines set {c['alias']} --owner <project>`)")
    else:
        role = ("owner" if target == m.get("recovery_owner") else "fallback" if target == m.get("recovery_fallback")
                else "coordinator of the reporting project" if target in (c.get("seen_by") or []) else "")
        acting = outagemsg.actor(target, role, passed)
        step = f"{target} checks and recovers it; record any live change with `ttp machines change add`"
    tried = [f"recovery page {i + 1} to {t}" for i, t in enumerate(targets[:stage])]
    task, due = tracked(c)
    return (f"Machine {c['alias']} {c['condition']}, seen by {', '.join(c.get('seen_by') or [])}: "
            f"{c.get('text') or 'no detail'}. Recovery page {stage + 1}. "
            + outagemsg.facts(float(c["first"]), now, acting, task=task, due=due, tried=tried,
                              next_step=step))


def _settle(sent: dict, done: bool) -> bool:
    """Mark the pending page `sent` delivered (done) or free to retry; False when it is no longer pending."""
    with _locked():
        for name, empty in ((ROUTES, {"conditions": {}}), (LEDGER, {"next": 1, "changes": []})):
            doc = _read(name, empty)
            items = doc.get("conditions", {}).values() if name == ROUTES else doc.get("changes", [])
            slots = [c.get(s) for c in items if isinstance(c, dict) for s in ("page", "task")]
            for slot in slots:
                pend = slot.get("pending") if isinstance(slot, dict) else None
                if not pend or pend.get("id") != sent.get("id"):
                    continue
                if done:
                    slot.pop("pending")
                    slot["delivered"] = sent["id"]
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

PROBE_BROKEN_KIND = "change_probe_broken"   # the event a broken until-probe queues for the coordinator


class Probes:
    """Runs the until-probes of the entries project `p` made, in the background, from its root."""

    def __init__(self, p, log: Callable[[str], None] = print):
        self.p, self.log = p, log
        self._procs: dict[int, tuple[subprocess.Popen, float, str]] = {}
        self._probed: dict[int, float] = {}
        self._errs: dict[int, Any] = {}   # id(proc) -> its stderr file
        self._reported: dict[int, str] = {}   # change id -> the broken kind ("exit 2", "timeout") already reported

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        passed = []
        for cid, (rc, probe, err) in ends.reap_probes(self._procs, self._errs, now, self.p).items():
            if rc in ends.NOT_YET_RCS:   # 255 only once ssh cannot reach the host it names
                self._reported.pop(cid, None)
            elif rc == 0 or detach.failed_check(probe, rc):   # the jobs it waits on ended, even if one failed
                passed.append((cid, probe, rc))
            else:   # broken, a timeout too (as ends.EndProbes reads it): said once per kind, not every round
                self._broken(cid, probe, rc, err, now)
        if passed:
            ids = {(cid, probe): rc for cid, probe, rc in passed}
            with _locked():
                doc = _read(LEDGER, {"next": 1, "changes": []})
                for c in doc.get("changes", []):
                    if (c.get("id"), c.get("probe")) in ids and not c.get("closed"):   # not a probe an edit replaced
                        c["probe_passed"] = now
                        if ids[(c["id"], c["probe"])]:
                            c["probe_job_failed"] = detach.failed_why(c["probe"], ids[(c["id"], c["probe"])], self.p.root)
                _write(LEDGER, doc)
        self._tasks(now)
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
                self._procs[cid] = (ends.spawn_probe(self.p, c["probe"], self._errs), now, c["probe"])
            except OSError as e:
                self.log(f"machine change #{cid}: until-probe could not start: {e}")

    def _tasks(self, now: float) -> None:
        """Mark this project's open entries whose until-task is over (task_ended), so any daemon's route
        pages it as overdue."""
        over = {(c["id"], c["until_task"]): why for c in changes() if c.get("by") == self.p.name
                and c.get("until_task") and not c.get("task_ended") and (why := task_over(self.p, c["until_task"]))}
        if not over:
            return
        with _locked():
            doc = _read(LEDGER, {"next": 1, "changes": []})
            for c in doc.get("changes", []):   # not an until-task an edit replaced meanwhile
                if (c.get("id"), c.get("until_task")) in over and not c.get("closed") and not c.get("task_ended"):
                    c.update(task_ended=now, task_end_why=over[(c["id"], c["until_task"])])
            _write(LEDGER, doc)

    def _broken(self, cid: int, probe: str, rc, err: str, now: float) -> None:
        """Log a broken until-probe and queue one low event for the coordinator, keyed on the change, the
        probe and how it broke (its exit code or "timeout"), never its stderr: once across restarts too."""
        code = rc if isinstance(rc, str) else f"exit {rc}"
        if self._reported.get(cid) == code:
            return
        self._reported[cid] = code
        why = f"{code}: {err}" if err else code
        self.log(f"machine change #{cid}: its until-probe is broken ({why[-500:]})")
        fp = f"{PROBE_BROKEN_KIND}:{cid}:{hashlib.sha256(probe.encode()).hexdigest()[:12]}:{code}"
        db = self.p.db
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return
        c = next((x for x in changes() if x.get("id") == cid), {})
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", PROBE_BROKEN_KIND, fp, "low",
              f"Machine change #{cid} on {c.get('alias', '?')} ({str(c.get('what', ''))[:200]}) ends when its "
              f"until-probe passes, but the probe is broken ({why[-300:]}; only 0 = over and 1, 75 or 255 = not "
              f"yet are valid, and it must finish within {ends.PROBE_TIMEOUT_S}s): {probe[:300]}. Unfixed, it is "
              f"flagged only once overdue ({ends.shown(float(c.get('expires') or now), self.p)}). Fix it: `ttp machines change edit {cid} --until-probe <command>`, or close "
              f"the change once it is undone.", "queued", c.get("task")))

    def stop(self) -> None:
        ends.stop_probes(self._procs, self._errs)


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
    return [line for _, line in digest_items(p, now)]


GAP_ITEM = "no_owner"   # digest_items' key of the line naming machines without a recovery owner


def _ends_itself(c: dict, p, now: float) -> bool:
    """Whether open entry `c` is another project's that ends by itself and needs nothing from `p`: a
    power-cycle intent, or a change with a set end time (no until-probe) that is not overdue yet. Its author
    is paged once it is overdue, and then it shows in full again."""
    return (c.get("by") != getattr(p, "name", None) and not overdue(c, now)
            and not c.get("probe"))


def digest_items(p, now: float | None = None) -> list[tuple[str, str]]:
    """digest_lines as (stable key, line): a change by its id, a condition by alias and condition, an
    outage by alias, so a reworded line keeps its key (the digest shows only new or reworded lines).
    Changes are clipped (DIGEST_LINE_CHARS); other projects' that end by themselves are one count per
    machine. Conditions and outages show in full."""
    now = time.time() if now is None else now
    mine = resource_machines(p)
    if not mine:
        return []
    known = machines.load()
    items, quiet = [], {}
    for c in changes(set(mine)):
        if _ends_itself(c, p, now):
            quiet[c["alias"]] = quiet.get(c["alias"], 0) + 1
        else:
            items.append((f"change:{c.get('id')}", change_line(c, now, p, clip=DIGEST_LINE_CHARS)))
    items += [(f"others:{a}", f"{a}: {n} more open change{'s' if n > 1 else ''} by other projects that end by "
               f"themselves (`ttp machines change list {a}`)") for a, n in sorted(quiet.items())]
    for key, c in conditions().items():
        if c.get("alias") in mine:
            owner = (known.get(c["alias"]) or {}).get("recovery_owner")
            held = (c.get("page") or {}).get("held")
            hush = (c.get("page") or {}).get("quiet")
            items.append((f"condition:{key}",
                          f"{c['alias']} {c['condition']} since {ends.shown(c['first'], p)} (seen by "
                          f"{', '.join(c.get('seen_by', []))}; recovery owner: {owner or 'none set'}"
                          + (f"; page held: {held['by']}'s heal check sees it healthy" if held else "")
                          + (f"; not escalated: {hush}" if hush else "")
                          + (f"; acting now: {acting_now(c)}" if not held and not hush else "") + ")"))
    items += [(f"outage:{a}", outage_line(a, o, p)) for a, o in sorted(outages(now).items()) if a in mine]
    named = list(p.db.kv(GAP_KV) or [])
    gaps = [a for a in mine if not (known.get(a) or {}).get("recovery_owner") and a not in named]
    if gaps:
        items.append((GAP_ITEM, "No recovery owner set for " + ", ".join(gaps) + ": a long down or held condition "
                      "there reaches nobody. Set one: `ttp machines set <alias> --owner <project> "
                      "[--fallback <project>]` (named once)."))
        p.db.set_kv(GAP_KV, named + gaps)
    return items


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


def task_lines(p) -> list[str]:
    """For the daily review: each open entry project `p` made whose until-task is over (done, failed,
    cancelled or gone) and that nobody undid and closed yet; one line each."""
    return [f"{change_line(c, where=p)} (task over: {why}; undo it and `ttp machines change close {c['id']}`, or "
            f"re-extend it for a continuation: `ttp machines change edit {c['id']} --until-task <id>`)"
            for c in changes() if c.get("by") == p.name and c.get("until_task")
            and (why := task_over(p, c["until_task"]))]
