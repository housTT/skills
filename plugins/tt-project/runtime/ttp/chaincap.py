# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Continue chains: caps per lineage, and the next hop the daemon queues itself.

A lineage here is a chain of tasks linked back by `continues:<id>` and `follows:<id>` labels (the
latter on a task_add that `continues` a done task, whatever its title) and by `continue:` follow-ups (a task titled `continue: ...` whose parent is the task it continues). Its first task is
the root; each later one is a hop. Per lineage the runtime counts hops, the spend of all its tasks,
and stalls: the hops, counted back from the newest, that failed or made no progress.

Progress (a done hop, against the task before it in the chain; the root against the base ref):
- new commits: a commit made since the hop was created, on its own branch or a branch of an
  earlier task in the lineage, that the base does not contain; or
- new passing evidence: a `metrics` entry in its hand-off that is new or changed against the
  previous task's.

Caps (`chains` in project.json; 0 turns one off): max_hops (DEFAULTS: 8), max_stalls (2 failed or
no-progress hops in a row) and max_usd (40 for the whole lineage). Once one is reached, or while
the lineage's area is under review.area_fail_cap (a reviewcap.REVIEW_AREA_EVENT within
reviewcap.AREA_WINDOW_S naming one of its tasks), a task_add that continues the lineage is refused,
and a CAP_EVENT makes the next coordinator turn a high-effort one (TRIGGER). The only way past is
the action's `reason` naming what changed (REASON_RE: a new approach, a rescoped spec, new
evidence), sent in a high-effort turn; that override is recorded as an OVERRIDE_EVENT.

Model-free hop (`chains.auto`, on by default): a done task whose only proposal is one `continue:`
follow-up, in a lineage below every cap, that made progress gets the follow-up queued by the daemon
with `continues` set (an AUTO_EVENT). The next hop starts from the base, not that task's branch, so a
code task whose branch has changes gets its review queued first; without one the coordinator
decides. That hop then waits (start_when `landed:#<task>`) until the reviewed commits are on the push
branch, unless they already are. The notice, and a local_only notice for a branch its review still
carries, go to the next digest (NOTICES_KEY) without waking the coordinator. Anything else still
goes to it.
"""
from __future__ import annotations

import json
import re
import time

from .db import DB, continues_id, load_result

CONT_TITLE = re.compile(r"(?i)^\s*continue\s*:")
REASON_RE = re.compile(r"(?i)\b(?:new|different|changed|other)\s+(?:approach|evidence|design|strategy)\b"
                       r"|\bre-?scop\w*|\bnarrow\w*\s+(?:the\s+)?scope|\bscope[sd]?\s+(?:down|narrow\w*)")
MIN_REASON = 20
DEFAULTS = {"max_hops": 8, "max_stalls": 2, "max_usd": 40.0, "auto": True}
CAP_EVENT = "continue_cap"
OVERRIDE_EVENT = "continue_cap_override"
AUTO_EVENT = "continue_auto"
TRIGGER = "continue chain over its cap"
FOLLOWS = "follows:"   # label of a task_add that `continues` a done task: a lineage link that takes nothing over
NOTICES_KEY = "chain_notices"   # kv: [{ts, text}]: hops the daemon queued, shown once in the next digest
MAX_CHAIN = 200
MAX_NOTICES = 30


def caps(cfg: dict) -> dict:
    c = cfg.get("chains") or {}
    out: dict = {"auto": c.get("auto", True) not in (False, 0, "off", "false")}
    for k in ("max_hops", "max_stalls", "max_usd"):
        try:
            out[k] = max(0.0, float(c.get(k, DEFAULTS[k])))
        except (TypeError, ValueError):
            out[k] = float(DEFAULTS[k])
    return out


def follows_id(task: dict) -> int | None:
    """The done task a follow-up was added to continue (its FOLLOWS label), whatever its title."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except ValueError:
        return None
    for lb in labels if isinstance(labels, list) else []:
        if isinstance(lb, str) and lb.startswith(FOLLOWS) and lb[len(FOLLOWS):].isdigit():
            return int(lb[len(FOLLOWS):])
    return None


def prev_of(task: dict) -> int | None:
    """The task `task` continues: its `continues:` or FOLLOWS label, or the parent of a `continue:` follow-up."""
    if (c := continues_id(task)) is not None or (c := follows_id(task)) is not None:
        return c
    if task.get("parent") and CONT_TITLE.match(task.get("title") or ""):
        return int(task["parent"])
    return None


def chain(db: DB, task_id: int) -> list[dict]:
    """The lineage ending at `task_id`, root first. Reviews are not hops: a review starts none."""
    out: list[dict] = []
    seen: set[int] = set()
    t = db.task(task_id)
    while t and t["id"] not in seen and len(out) < MAX_CHAIN and t["kind"] != "review":
        out.append(t)
        seen.add(t["id"])
        i = prev_of(t)
        t = db.task(i) if i is not None else None
    return out[::-1]


def _metrics(task: dict | None) -> dict:
    m = load_result((task or {}).get("result")).get("metrics")
    return m if isinstance(m, dict) else {}


def progress(p, ch: list[dict], i: int) -> str:
    """Why hop `ch[i]` made progress (see the module's rule), or '' when it did not."""
    from . import worktree
    hop = ch[i]
    if hop["status"] != "done":
        return ""
    try:
        base = worktree.resolve_base(p)
        refs = [b for t in ch[:i + 1] if (b := t.get("branch"))
                and worktree._git(p.root, "rev-parse", "--verify", "--quiet", f"{b}^{{commit}}", check=False)]
        if refs:
            n = worktree._git(p.root, "rev-list", "--count", f"--since=@{int(hop['created'] or 0)}", *refs,
                              f"^{base}", check=False)
            if n.isdigit() and int(n):
                return f"{n} new commit{'' if n == '1' else 's'}"
    except Exception:   # unreadable branches: only the evidence can show progress
        pass
    old = _metrics(ch[i - 1]) if i else {}
    new = sorted(str(k) for k, v in _metrics(hop).items() if k not in old or old[k] != v)
    return "new evidence: " + ", ".join(new[:5]) if new else ""


def stalls(p, ch: list[dict]) -> int:
    """Hops in a row, back from the newest, that failed or made no progress (the root is no hop)."""
    n = 0
    for i in range(len(ch) - 1, 0, -1):
        if progress(p, ch, i):
            break
        n += 1
    return n


def spend(ch: list[dict]) -> float:
    return sum(float(t.get("spent_usd") or 0) for t in ch)


def area_held(db: DB, ch: list[dict], now: float | None = None) -> int | None:
    """The root of a review area under review.area_fail_cap that holds one of the chain's tasks."""
    from . import reviewcap
    now = time.time() if now is None else now
    ids = {t["id"] for t in ch}
    for r in db.q("SELECT data FROM events WHERE kind=? AND ts>? ORDER BY ts DESC",
                  (reviewcap.REVIEW_AREA_EVENT, now - reviewcap.AREA_WINDOW_S)):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict) and ids & {int(x) for x in d.get("tasks") or [] if str(x).isdigit()}:
            return int(d.get("root") or 0)
    return None


def refusal(p, cfg: dict, ch: list[dict]) -> str:
    """Why another hop after the chain `ch` is refused, '' when it may follow."""
    if not ch:
        return ""
    c, root = caps(cfg), ch[0]
    hops, usd = len(ch) - 1, spend(ch)
    head = f"the continue chain of #{root['id']} ({str(root['title'])[:80]}) "
    if c["max_hops"] and hops + 1 > c["max_hops"]:
        return head + f"has {hops} hops; chains.max_hops is {c['max_hops']:g}"
    if c["max_usd"] and usd >= c["max_usd"]:
        return head + f"spent ${usd:.2f}; chains.max_usd is ${c['max_usd']:g}"
    if (a := area_held(p.db, ch)) is not None:
        return head + f"is in the review area of #{a}, under review.area_fail_cap (re-plan it first)"
    if c["max_stalls"] and (n := stalls(p, ch)) >= c["max_stalls"]:
        return head + f"ended with {n} failed or no-progress hops in a row; chains.max_stalls is {c['max_stalls']:g}"
    return ""


def reason_ok(reason) -> bool:
    """A reason that names what changed: a new approach, a rescoped spec or new evidence."""
    r = str(reason or "").strip()
    return len(r) >= MIN_REASON and bool(REASON_RE.search(r))


def check_add(p, cfg: dict, prev: int, reason, high_effort: bool) -> str:
    """For a task_add continuing `prev`: '' when its lineage is under every cap, a note when `reason`
    takes it past one in a high-effort turn; otherwise raises ValueError, with a CAP_EVENT for the
    next turn (once per lineage and hop count)."""
    db = p.db
    ch = chain(db, prev)
    why = refusal(p, cfg, ch)
    if not why:
        return ""
    data = {"root": ch[0]["id"], "tip": prev, "hops": len(ch) - 1, "usd": round(spend(ch), 2), "why": why}
    if reason_ok(reason) and high_effort:
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", OVERRIDE_EVENT, "low", f"{why}; passed with reason: {str(reason)[:300]}",
              json.dumps({**data, "reason": str(reason)[:300]}), "handled", prev))
        return f"task_add continues #{prev} past its cap ({why}) with reason: {str(reason)[:200]}"
    fp = f"{CAP_EVENT}:{ch[0]['id']}:{len(ch) - 1}"
    if not db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
             (time.time(), "coordinator", CAP_EVENT, fp, "normal",
              f"{TRIGGER}: {why}. Another hop is refused. Re-plan: narrow the scope, change the approach, or stop "
              f"and report what is left; a task_add that continues it passes only with a `reason` naming what "
              f"changed, in a high-effort turn.", json.dumps(data), "queued", prev))
    hint = ("its `reason` does not name what changed (a new approach, a rescoped spec, new evidence)"
            if not reason_ok(reason) else "a `reason` passes the cap only in a high-effort turn")
    raise ValueError(f"task_add continues #{prev} refused: {why}; {hint}")


def hop_ready(p, cfg: dict, task: dict, fup: dict) -> str:
    """Why `fup`, the only (`continue:`) follow-up of done `task`, may be queued by the daemon (`task`
    made progress and its lineage is under every cap), '' when it may not."""
    if not caps(cfg)["auto"] or not CONT_TITLE.match(str(fup.get("title") or "")):
        return ""
    ch = chain(p.db, task["id"])
    if not ch or refusal(p, cfg, ch):
        return ""
    return progress(p, ch, len(ch) - 1)


def auto_hop(p, cfg: dict, task: dict, fup: dict, why: str | None = None,
             wait_landed: bool = False) -> tuple[int, str] | None:
    """Queue `fup` when hop_ready (`why`, when the caller checked it already): (new task id, why it
    progressed), else None. With `wait_landed` (a code hop whose commits are not on the push branch
    yet), the new hop starts only once they landed (start_when `landed:#<task>`): it starts from the
    branch tip, so before that it would lack them and redo them or conflict with them at push time."""
    from . import coordinator as coord
    db = p.db
    why = hop_ready(p, cfg, task, fup) if why is None else why
    if not why:
        return None
    ch = chain(db, task["id"])
    title = str(fup["title"]).strip()[:200]
    action = {"type": "task_add", "title": title, "spec": str(fup.get("spec") or ""), "kind": task["kind"],
              "tier": task["tier"], "priority": task["priority"], "budget_usd": task["budget_usd"],
              "continues": task["id"]}
    if wait_landed:
        action.update(start_when=f"landed:#{task['id']}", why=f"#{task['id']}'s commits land on the push branch")
    saved = db.kv(coord.NOTES_KEY)   # the coordinator's own notes for its next digest stay as they were
    try:
        problems = coord.apply(p, [action])
    finally:
        db.set_kv(coord.NOTES_KEY, saved)
    new = db.one("SELECT id FROM tasks WHERE parent=? AND title=? ORDER BY id DESC", (task["id"], title))
    if problems or not new:
        return None
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (time.time(), f"task:{task['id']}", AUTO_EVENT, "low", f"#{new['id']} {title} queued by the daemon ({why})",
          json.dumps({"root": ch[0]["id"], "next": new["id"], "hops": len(ch), "why": why}), "handled", task["id"]))
    return new["id"], why


def auto_continued(db: DB, task_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (AUTO_EVENT, task_id)))


def add_notice(db: DB, text: str) -> None:
    notes = [n for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]
    db.set_kv(NOTICES_KEY, (notes + [{"ts": time.time(), "text": text[:800]}])[-MAX_NOTICES:])


def notices(db: DB) -> list[str]:
    return [str(n.get("text") or "") for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]


def clear_notices(db: DB, before: float) -> None:
    """Drop the notices a turn that started at `before` saw; later ones wait for the next turn."""
    notes = [n for n in db.kv(NOTICES_KEY, []) or [] if isinstance(n, dict)]
    keep = [n for n in notes if float(n.get("ts") or 0) > before]
    if keep != notes:
        db.set_kv(NOTICES_KEY, keep or None)


def lineages(db: DB, since: float) -> list[dict]:
    """Each lineage with a hop active since `since`: {root, title, hops, usd, auto, refused, overridden, tip}."""
    out: dict[int, dict] = {}
    for t in db.q("SELECT * FROM tasks WHERE COALESCE(updated, created)>=? ORDER BY id", (since,)):
        if prev_of(t) is None:
            continue
        ch = chain(db, t["id"])
        if len(ch) < 2 or ch[0]["id"] in out and out[ch[0]["id"]]["hops"] >= len(ch) - 1:
            continue
        out[ch[0]["id"]] = {"root": ch[0]["id"], "title": str(ch[0]["title"]), "hops": len(ch) - 1,
                            "usd": spend(ch), "tip": ch[-1]["id"], "status": ch[-1]["status"],
                            "auto": 0, "refused": 0, "overridden": 0}
    for kind, key in ((AUTO_EVENT, "auto"), (CAP_EVENT, "refused"), (OVERRIDE_EVENT, "overridden")):
        for r in db.q("SELECT data FROM events WHERE kind=? AND ts>=?", (kind, since)):
            try:
                root = int(json.loads(r["data"] or "{}").get("root"))
            except (ValueError, TypeError, AttributeError):
                continue
            if root in out:
                out[root][key] += 1
    return sorted(out.values(), key=lambda x: -x["usd"])


def audit_line(db: DB, since: float, top: int = 8) -> str:
    """The self-efficiency audit's line: hops and spend per lineage, and refusals; '' with none."""
    ls = lineages(db, since)
    if not ls:
        return ""
    refused = sum(x["refused"] for x in ls)
    return (f"continue chains: {len(ls)} lineage(s), {sum(x['hops'] for x in ls)} hops, "
            f"${sum(x['usd'] for x in ls):.2f}, {sum(x['auto'] for x in ls)} hops queued by the daemon, "
            f"{refused} refused by a cap, {sum(x['overridden'] for x in ls)} passed with a reason: " + "; ".join(
                f"#{x['root']} {x['title'][:50]}: {x['hops']} hops to #{x['tip']} ({x['status']}), ${x['usd']:.2f}"
                + (f", {x['auto']} auto" if x["auto"] else "") + (f", {x['refused']} refused" if x["refused"] else "")
                + (f", {x['overridden']} overridden" if x["overridden"] else "") for x in ls[:top])
            + (f"; +{len(ls) - top} more" if len(ls) > top else ""))
