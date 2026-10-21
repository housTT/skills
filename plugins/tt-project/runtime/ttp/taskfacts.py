# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Facts at send time: the live record of the tasks a spec or a note names.

A coordinator's hand-back spec, or a worker's note to another project (`ttp note --to`), often
states another task's status ("#12's fix is not pushed yet", "the switch is not live yet") from
memory, and is stale by the time it is read. So the text is not the authority: each task it names
(`#<id>`, not a message, PR, issue or machine change number) gets one line read from the records when
the text is sent or dispatched: its status, the commit the push queue, a detached push or a
supersession recorded for it or a task carrying it on (landed.task_shas), a push still queued, and
the machine changes it recorded in the change ledger (machine_ledger). The worker prompt carries
them as a block (prompt_block, built at dispatch), a note to another project as a suffix
(note_suffix, at send). Workers check stated status against them before acting: the records win.
"""
from __future__ import annotations

import re
import time

MAX_TASKS = 6          # tasks named in one text that get a line
TITLE_CHARS = 70
WHAT_CHARS = 100
LINE_CHARS = 360
# `#<id>` as a task reference; the word before it says when it is another kind of number.
_REF = re.compile(r"(?<![\w#&])#(\d{1,7})(?!\d)")
_NOT_TASK = re.compile(r"\b(?:user\s+)?(?:msgs?|messages?|prs?|pulls?|pull\s+requests?|issues?|changes?|"
                       r"entry|entries|notes?|steps?|items?|rows?|lines?|comments?|no|number|batch(?:es)?)\.?\s*$",
                       re.I)


def ids_in(text: str, skip=()) -> list[int]:
    """The task ids `text` names as `#<id>`, in order, once each, without `skip`, at most MAX_TASKS."""
    out: list[int] = []
    for m in _REF.finditer(text or ""):
        if _NOT_TASK.search(text[max(0, m.start() - 24):m.start()]):
            continue
        n = int(m.group(1))
        if n not in out and n not in skip:
            out.append(n)
    return out[:MAX_TASKS]


def _clip(text, chars: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= chars else text[:chars - 1] + "…"


def _queued(p, carry) -> str:
    """The newest push queue row of `carry` (the task and its carriers) not pushed yet, as a few words."""
    try:
        rows = p.db.q("SELECT task, status FROM push_queue WHERE task IN (%s) ORDER BY id DESC LIMIT 1"
                      % ",".join("?" * len(carry)), list(carry))
    except Exception:
        return ""
    if not rows or rows[0]["status"] in ("pushed", "landed"):
        return ""
    r = rows[0]
    return f"push queue: {r['status']}" + (f" (through #{r['task']})" if r["task"] != min(carry) else "")


def _changes(p, tid: int) -> list[str]:
    from . import machine_ledger
    try:
        rows = machine_ledger.changes(open_only=False)
    except Exception:
        return []
    out = []
    for c in rows:
        if c.get("by") != p.name or str(c.get("task")) != str(tid):
            continue
        state = "open" if not c.get("closed") else f"closed: {_clip(c.get('closed_why') or 'closed', 40)}"
        out.append(f"machine change #{c.get('id')} on {c.get('alias')} ({state}): {_clip(c.get('what'), WHAT_CHARS)}")
    return out[-3:]


def fact_line(p, tid: int) -> str:
    """One line of task `tid`'s live record ('' when there is no such task)."""
    from . import landed
    try:
        t = p.db.task(int(tid))
    except Exception:
        return ""
    if not t:
        return ""
    parts = [f"#{tid} {_clip(t.get('title'), TITLE_CHARS)} ({t.get('kind')}, {t.get('status')})"]
    try:
        shas, why = landed.task_shas(p, int(tid))
    except Exception:
        shas, why = [], ""
    if shas and not why.startswith("its branch"):
        parts.append(f"on the push branch as {', '.join(s[:10] for s in shas[-3:])} ({why})")
    else:
        queued = _queued(p, landed.carriers(p.db, int(tid)))
        parts.append(queued or "no push or landing recorded")
    try:
        sup = landed.superseded(p.db, [int(tid)]).get(int(tid), [])
    except Exception:
        sup = []
    if sup and not (shas and why.startswith("the commit recorded")):
        parts.append("superseded by " + ", ".join(s[:10] for _, s in sup[-2:]))
    parts += _changes(p, int(tid))
    return _clip("; ".join(parts), LINE_CHARS)


def lines(p, text: str, skip=()) -> list[str]:
    return [x for x in (fact_line(p, i) for i in ids_in(text, skip)) if x]


def prompt_block(p, task: dict, now: float | None = None) -> str:
    """The worker prompt's block for the other tasks its title and spec name ('' if none)."""
    from . import timefmt
    try:
        got = lines(p, f"{task.get('title') or ''}\n{task.get('spec') or ''}", skip={int(task.get("id") or 0)})
    except Exception:
        return ""
    if not got:
        return ""
    at = timefmt.short(time.time() if now is None else now, p, now)
    return ("## Facts at send time\n"
            f"Read from this project's task records and machine change ledger at {at}, when this task was "
            "dispatched. The spec was written earlier: where it states one of these tasks' status (pushed, "
            "landed, superseded, a machine change made or live), check it against these lines before acting. "
            f"The records win; recheck with `ttp landed --task <id>` or `ttp task {p.name} show <id>`.\n"
            + "\n".join(f"- {x}" for x in got))


def note_suffix(p, text: str, limit: int) -> str:
    """`text` (a note to another project, one line) with its named tasks' live records appended, as many
    as fit within `limit` characters; `text` itself when none do or nothing is named."""
    try:
        got = lines(p, text)
    except Exception:
        return text
    while got:
        out = f"{text} [facts at send time from {p.name}'s records: {' | '.join(got)}]"
        if len(out) <= limit:
            return out
        got.pop()
    return text
