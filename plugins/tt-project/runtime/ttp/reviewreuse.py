"""One head gets one review. A review records, as it starts, the head commit it reviews and the
project's check set (a label); once it is accepted (`done`: a changes_needed verdict is stored
failed), a later review of the same head under the same checks reuses its verdict instead of
running again. The daemon looks before it queues or starts a review, and the coordinator's
task_add of such a review is refused unless forced with a reason. A different head, other checks
or an earlier review that failed or asked for changes still get a review. A review that would push
(or a code task's review in a project that pushes) takes only a verdict whose head was delivered:
a review-only verdict never pushed it, so reusing it would drop the delivery."""
from __future__ import annotations

import hashlib
import json
import re

HEAD_LABEL = "review_head:"     # review_head:<40-hex sha>@<check set key>
FORCED_LABEL = "review_forced"  # a review task_add forced past an accepted one: it runs
SUMMARY_CHARS = 600
_LABEL = re.compile(r"review_head:([0-9a-f]{40})@([0-9a-f]+)")


def check_key(cfg: dict) -> str:
    """A short key for the project's check set (delivery.push_checks): other checks, other key."""
    checks = (cfg.get("delivery") or {}).get("push_checks")
    return hashlib.sha1(json.dumps(checks, sort_keys=True, default=str).encode()).hexdigest()[:12]


def head_label(sha: str, key: str) -> str:
    return f"{HEAD_LABEL}{sha}@{key}"


def recorded(task: dict) -> tuple[str, str] | None:
    """(head, check key) a review recorded as it started, or None."""
    for lb in json.loads(task.get("labels") or "[]"):
        m = _LABEL.fullmatch(lb) if isinstance(lb, str) else None
        if m:
            return m.group(1), m.group(2)
    return None


def with_head(labels: list, sha: str, key: str) -> list:
    """`labels` with the head label set to `sha` under `key` (a later start replaces the earlier one)."""
    return [lb for lb in labels if not (isinstance(lb, str) and lb.startswith(HEAD_LABEL))] + [head_label(sha, key)]


def pushes(cfg: dict, labels: list | tuple = ()) -> bool:
    """Whether a review with `labels` (a code task's review: no labels) would deliver its head to the
    push branch: the project pushes to one and the review is not labelled review only."""
    from .project import push_allowed
    from .push import REVIEW_ONLY_LABEL
    d = cfg.get("delivery") or {}
    return push_allowed(d) and bool(str(d.get("push_branch") or "").strip()) and REVIEW_ONLY_LABEL not in labels


def delivered(p, sha: str) -> bool:
    """Whether head `sha` is on its way or already there: a live push_queue row for it, or the push
    target's tip as last fetched contains it."""
    from . import pushq
    if p.db.one("SELECT 1 FROM push_queue WHERE head=? AND status IN ('approved','batched','pushed',"
                "'landed') LIMIT 1", (sha,)):
        return True
    tgt = pushq.target(p)
    tip = pushq._tracking_tip(p, *tgt) if tgt else ""
    return bool(tip) and pushq._contains(p, [tip], sha)


def accepted(db, sha: str, key: str, skip: int | None = None, p=None) -> dict | None:
    """The review that first accepted `sha` under check set `key` (a review that reused one points at
    it), or None. Only a `done` review counts: failed, changes_needed and cancelled ones do not. With
    project `p` the caller would push `sha` (pushes): only a delivered head counts, as a review-only
    verdict stands in for no push."""
    if not sha or (p is not None and not delivered(p, sha)):
        return None
    for r in db.q("SELECT * FROM tasks WHERE kind='review' AND status='done' AND labels LIKE ? ORDER BY id",
                  (f'%"{head_label(sha, key)}"%',)):
        if r["id"] == skip:
            continue
        first = _result(r).get("reused_review")
        orig = db.task(first) if isinstance(first, int) else None
        return orig if orig and orig["status"] == "done" else r
    return None


def _result(task: dict) -> dict:
    try:
        v = json.loads(task.get("result") or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def rejection(prior: dict, sha: str) -> str:
    """Why task_add refuses a review of a head `prior` already accepted."""
    return (f"head {sha[:12]} was already accepted by review #{prior['id']} under the same checks: its verdict "
            f"stands (dependents can take it from #{prior['id']}); review a new head, or add force: true with "
            f"a reason saying why this head needs another review")


def reuse(db, review: dict, prior: dict, sha: str, key: str) -> str:
    """Close `review` with `prior`'s verdict, without a run: its result names `prior` and carries its
    summary, and each open task waiting on it gets that as the review's evidence in its spec. The
    event text, for the caller to post."""
    from .db import dependency_ids
    clip = str(_result(prior).get("summary") or "")[:SUMMARY_CHARS]
    summary = (f"Reused the verdict of review #{prior['id']}, which accepted head {sha[:12]} under the same "
               f"checks: no new run." + (f" Its summary: {clip}" if clip else ""))
    labels = with_head(json.loads(review.get("labels") or "[]"), sha, key)
    db.update_task(review["id"], status="done", labels=labels, blocked_reason=None,
                   result=json.dumps({"status": "done", "summary": summary, "reused_review": prior["id"],
                                      "reviewed_head": sha}))
    line = (f"\nReview #{review['id']} (which this waits on) reused the accepted verdict of review #{prior['id']} "
            f"on head {sha[:12]}; its evidence is review #{prior['id']}'s result.")
    for t in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled') "
                  "AND depends_on NOT IN ('', '[]')"):
        if review["id"] in dependency_ids(t) and line.strip() not in (t["spec"] or ""):
            db.update_task(t["id"], spec=(t["spec"] or "") + line)
    return f"#{review['id']} {review['title']} → done (no run): {summary}"
