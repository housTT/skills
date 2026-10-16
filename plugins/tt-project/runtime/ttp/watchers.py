# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Built-in, model-free watchers. Each reports only CHANGES, so an unchanged world costs nothing.

- `prs`: every pull request a task opened — draft/ready, CI result, new review activity, approval,
  merge. Emits events straight to the coordinator (they are already specific and actionable).
  It never puts a PR back in draft and never touches its reviewers: the user may share the
  harness's GitHub account, and their own actions there win. A PR that leaves draft without the
  user's recorded approval (prguard) is checked against the runs' gh log: if a run marked it ready
  or requested reviewers on it, a high alert names the run; otherwise the user did it on GitHub and
  that is recorded as their approval, with one line in the feed. Open PRs' findings
  (failing CI, bot review comments neither fixed nor answered) are recorded for prguard and are
  work: the coordinator hears when a PR has some (to queue a fix task) and when it is clean (to ask
  the user for a review). A bot's re-post of a finding it already made (after each push, say) is one
  finding, not new work, and an empty COMMENTED review that only wraps the runs' own or a bot's inline
  comments is no activity (a person's inline-only review, or one whose comments are unknown, counts; a failed read keeps the last known activity). A move of an open PR's CI alone raises no pr_changed: those reports cover it.
- `logfile`: tails files named in the payload and screens new lines (rules, then Jev if enabled).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import prguard
from . import screen as scr

DAY = 86400.0
GH_LOG_MARGIN_S = 3600.0   # runs' gh calls this long before a PR was last seen in draft still count


def run_builtin(daemon, name: str, payload: dict) -> str:
    kind = payload.get("builtin") or name
    if kind == "prs":
        return watch_prs(daemon)
    if kind == "logfile":
        return watch_logs(daemon, payload)
    return f"unknown builtin {kind}"


def _gh(args: list[str], cwd: str) -> dict | None:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


TTP_MARKER = "<!-- ttp -->"


def _own(c: dict, pr: dict) -> bool:
    """A comment or review a tt-project run posted: it carries the hidden marker, whatever the author
    (the runs' gh account may not be the one that opened the PR). Unmarked comments always count."""
    return TTP_MARKER in str(c.get("body") or "")


def _reply_wrapper(r: dict) -> bool:
    """An empty COMMENTED review GitHub adds around inline comments that are all the runs' own replies
    (marked) or a bot's: no activity. A review whose inline comments are unknown (gh pr view carries
    none; _with_review_comments adds them) or include a person's unmarked one is a review and counts."""
    if str(r.get("state") or "").upper() != "COMMENTED" or str(r.get("body") or "").strip():
        return False
    cs = r.get("comments")
    nodes = (cs.get("nodes") or []) if isinstance(cs, dict) else []
    if not nodes or int(cs.get("totalCount") or 0) > len(nodes):
        return False
    return all(_is_bot(c.get("author")) or TTP_MARKER in str(c.get("body") or "") for c in nodes)


def _with_review_comments(pr: dict, data: dict | None) -> dict:
    """The PR with each review's inline comments from the GraphQL answer (BOT_QUERY), matched by id."""
    nodes = ((((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}) \
        .get("reviews") or {}
    by_id = {n["id"]: n.get("comments") for n in nodes.get("nodes") or [] if n.get("id")}
    if not by_id:
        return pr
    return {**pr, "reviews": [{**r, "comments": by_id[r["id"]]} if r.get("id") in by_id else r
                              for r in pr.get("reviews") or []]}


def pr_signature(pr: dict) -> dict:
    checks = pr.get("statusCheckRollup") or []
    states = sorted({(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in checks})
    failing = sorted(c.get("name") or c.get("context") or "?" for c in checks
                     if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT",
                                                                                 "CANCELLED", "ACTION_REQUIRED"))
    pending = any((c.get("status") or c.get("state") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
                  for c in checks)
    reviews = [r for r in pr.get("reviews") or [] if not _reply_wrapper(r)]
    human = [c for c in (pr.get("comments") or []) + reviews
             if not ((c.get("author") or {}).get("login", "").endswith("[bot]")) and not _own(c, pr)]
    activity = hashlib.sha1(json.dumps([(c.get("author") or {}).get("login", "") + str(c.get("body", ""))[:200]
                                        + str(c.get("state", "")) for c in human]).encode()).hexdigest()[:12]
    return {"state": pr.get("state"), "draft": pr.get("isDraft"), "decision": pr.get("reviewDecision"),
            "mergeable": pr.get("mergeable"), "checks": "pending" if pending else ("failing" if failing else
                                                                                  ("passing" if states else "none")),
            "failing": failing[:10], "activity": activity, "n_human": len(human)}


def watch_prs(daemon) -> str:
    db, root = daemon.p.db, str(daemon.p.root)
    rows = db.q("SELECT id, title, pr_url, status FROM tasks WHERE pr_url IS NOT NULL AND pr_url!='' "
                "AND status NOT IN ('cancelled')")
    seen = db.kv("pr_signatures", {})
    # PRs already out of draft when this check first ran predate the guard and are not flagged.
    before = db.kv(prguard.PREDATES_KEY)
    if before is None:
        before = [u for u, s in seen.items() if s.get("state") == "OPEN" and s.get("draft") is False]
    before = set(before)
    flagged = dict(db.kv(prguard.UNAPPROVED_KEY, {}) or {})   # a PR gh could not read keeps its flag
    findings = dict(db.kv(prguard.FINDINGS_KEY, {}) or {})
    heads = dict(db.kv(prguard.HEADS_KEY, {}) or {})
    drafts = dict(db.kv(prguard.DRAFT_SEEN_KEY, {}) or {})
    changed = 0
    for t in rows:
        pr = _gh(["pr", "view", t["pr_url"], "--json", "state,isDraft,mergeable,reviewDecision,statusCheckRollup,"
                  "comments,reviews,url,title,author,headRefOid,headRefName"], root)
        if pr is None:
            continue
        key = prguard.pr_key(pr.get("url") or t["pr_url"])
        if key and pr.get("headRefOid") and (heads.get(key) or {}).get("sha") != pr["headRefOid"]:
            heads[key] = {"sha": pr["headRefOid"], "seen": time.time()}   # what an approval binds to
        # One GraphQL read serves the findings and which empty reviews only wrap the runs' replies.
        data = None
        wrappers = any(str(r.get("state") or "").upper() == "COMMENTED" and not str(r.get("body") or "").strip()
                       for r in pr.get("reviews") or [])
        if key and (pr.get("state") == "OPEN" or wrappers):
            owner, rest = key.split("/", 1)
            name, number = rest.split("#")
            data = _gh(["api", "graphql", "-f", f"query={BOT_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                        "-F", f"number={number}"], root)
        pr = _with_review_comments(pr, data)
        sig = pr_signature(pr)
        _check_unapproved(daemon, t, pr, sig, before, flagged, drafts)
        _check_findings(daemon, t, pr, sig, findings, data)
        old = seen.get(t["pr_url"])
        if old and sig["mergeable"] == "UNKNOWN" and old.get("mergeable") not in (None, "UNKNOWN"):
            sig = {**sig, "mergeable": old["mergeable"]}   # GitHub is recomputing: keep the last known value
        if old and wrappers and data is None:
            # The inline comments could not be read: keep the last known activity, so a failed read and
            # the next good one raise no pr_changed. A first sighting counts the unknown wrappers.
            sig = {**sig, "activity": old.get("activity"), "n_human": old.get("n_human")}
        if sig == old:
            continue
        seen[t["pr_url"]] = sig
        if old is not None and {k for k, v in sig.items() if old.get(k) != v} == {"mergeable"} \
                and "UNKNOWN" in (old.get("mergeable"), sig["mergeable"]):
            continue   # only mergeable moved to or from UNKNOWN: not news
        changed += 1
        if old is None:
            continue   # first sighting records a baseline; nothing new has happened yet
        if sig["state"] == "OPEN" and {k for k, v in sig.items() if old.get(k) != v} <= {"checks", "failing"}:
            continue   # only CI moved on an open PR: _check_findings reports each outcome once
        what =[f"{k}: {old.get(k)} → {v}" for k, v in sig.items() if old.get(k) != v and k != "activity"]
        if sig["activity"] != old.get("activity"):
            what.append(f"new review/comment activity ({sig['n_human']} human items)")
        sev = "high" if sig["state"] == "MERGED" or sig["decision"] == "CHANGES_REQUESTED" else "normal"
        text = f"PR for task #{t['id']} ({pr.get('url')}): " + "; ".join(what)
        # An active 'pr' mute covering it: recorded and counted there, but it does not wake the coordinator.
        status = "muted" if scr.count_muted(db, "pr", text, sev) else "queued"
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "pr", "pr_changed", sev, text, status, t["id"]))
    # A flag whose task is gone (cancelled, say) or whose PR gh has not read for a day is dropped,
    # so its alert clears.
    tasks, now = {t["id"] for t in rows}, time.time()
    flagged = {k: v for k, v in flagged.items()
               if v.get("task") in tasks and now - float(v.get("seen") or v.get("since") or 0) < DAY}
    db.set_kv("pr_signatures", seen)
    db.set_kv(prguard.PREDATES_KEY, sorted(before))
    db.set_kv(prguard.UNAPPROVED_KEY, flagged)
    db.set_kv(prguard.FINDINGS_KEY, findings)
    db.set_kv(prguard.HEADS_KEY, heads)
    keys = {prguard.pr_key(t["pr_url"]) for t in rows}
    db.set_kv(prguard.DRAFT_SEEN_KEY, {k: v for k, v in drafts.items() if k in keys})
    unapproved = f", {len(flagged)} out of draft unapproved" if flagged else ""
    return f"ok ({len(rows)} PRs, {changed} changed{unapproved})"


def _check_unapproved(daemon, t: dict, pr: dict, sig: dict, before: set, flagged: dict, drafts: dict) -> None:
    """Handle a PR that is out of draft without the user's recorded approval. Nothing here changes
    the PR. If the runs' gh log (prguard.GH_LOG) shows a run marking it ready or requesting reviewers
    on it since it was last seen in draft, a restriction is at risk: raise a high alert naming the run
    and, the first time, queue an event for the coordinator. The flag (and the alert) clears once the
    PR is back in draft, closed, merged or approved. Otherwise the user took it out of draft on GitHub:
    record that as their approval and log one feed line."""
    db, url = daemon.p.db, t["pr_url"]
    key = prguard.pr_key(pr.get("url") or url)
    if not key:
        return
    now = time.time()
    if sig["state"] != "OPEN" or sig["draft"] is not False:
        before.discard(url)
        flagged.pop(key, None)
        if sig["draft"]:
            drafts[key] = now
            prguard.drop_spent(db, key)   # back in draft: leaving it again needs a fresh yes
        return
    if url in before or prguard.approved(db, key):
        flagged.pop(key, None)
        return
    shown = pr.get("url") or url
    if "run" not in (flagged.get(key) or {}):   # a flag from before the gh log is judged again
        since = float(drafts.get(key) or now - DAY) - GH_LOG_MARGIN_S
        calls = prguard.worker_calls(daemon.p.state, key, since)
        if not calls:
            flagged.pop(key, None)
            prguard.approve_from_github(db, key, pr.get("headRefOid"), now)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, "pr", "pr_ready_by_user", "low",
                  f"PR for task #{t['id']} ({shown}) left draft on GitHub, not through any run's gh: recorded as "
                  f"the user's own approval. Nothing was changed on the PR.", "handled", t["id"]))
            return
        last = ([c for c in calls if key in (c.get("prs") or [])] or calls)[-1]   # one that names it first
        did = "marked it ready" if last.get("action") == "ready" else "requested reviewers on it"
        how = "the harness's gh refused it" if last.get("refused") else f"gh exited {last.get('rc')}"
        run = f"run {last.get('run')}" + (f" of task #{last['task']}" if last.get("task") else "")
        flagged[key] = {"task": t["id"], "url": shown, "run": last.get("run"), "since": now, "seen": now,
                        "why": f"{run} {did} ({how}): {str(last.get('cmd') or '')[:160]}"}
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "pr", "pr_unapproved_ready", "high",
              f"PR for task #{t['id']} ({shown}) is out of draft without the user's recorded approval, and "
              f"{flagged[key]['why']}. A restriction is at risk: find out how that run got around the guard. "
              f"pr-watch did not put the PR back in draft and never will; leave its draft state and reviewers "
              f"to the user.", "queued", t["id"]))
    flagged[key]["seen"] = now
    daemon.alert(f"{prguard.UNAPPROVED_ALERT}:{key}",
                 f"{shown} (task #{t['id']}) left draft without your recorded approval, and "
                 f"{flagged[key]['why']}. Nothing on the PR was changed; whether it stays ready is your call.",
                 "high", every_s=DAY)


BOT_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
             "pullRequest(number:$number){"
             "comments(last:100){nodes{author{__typename login} body createdAt url}}"
             "reviews(last:100){nodes{id author{__typename login} state body submittedAt url "
             "comments(first:50){totalCount nodes{author{__typename login} body}}}}"
             "reviewThreads(first:100){nodes{isResolved isOutdated path "
             "comments(first:50){nodes{author{__typename login} body url}}}}}}}")


def _is_bot(author: dict | None) -> bool:
    a = author or {}
    return a.get("__typename") == "Bot" or str(a.get("login", "")).endswith("[bot]")


_URL_RE = re.compile(r"https?://\S+")
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_HIDDEN_RE = re.compile(r"<!--.*?-->", re.S)
_RULE_RE = re.compile(r"\brule[ _-]?(?:id|name)?\s*[:=#]\s*[`'\"]?([\w./-]+)", re.I)


def finding_key(c: dict, path: str | None = None) -> str:
    """What makes two bot findings the same one: the file and the comment's text, with links, commit
    ids and hidden markup (which bots vary per push) left out, plus a rule id the text names. A
    comment without a body is only itself."""
    body = str(c.get("body") or "")
    if not body.strip():
        return "url:" + str(c.get("url") or id(c))
    m = _RULE_RE.search(body)
    text = _SHA_RE.sub(" ", _URL_RE.sub(" ", _HIDDEN_RE.sub(" ", body)).lower())
    text = " ".join(re.sub(r"\W+", " ", text).split())
    return hashlib.sha1(json.dumps([path or c.get("path") or "", m.group(1).lower() if m else "", text])
                        .encode()).hexdigest()[:12]


def bot_findings(data: dict) -> list[tuple[str, str]]:
    """(finding_key, URL) of bot review findings on a PR that are neither fixed nor answered: a review
    thread a bot started that is unresolved, not outdated and has no reply from a person; a bot's
    top-level comment, or review asking for changes, with no comment from a person after it. A bot
    re-posting a finding (same key) is one finding, listed once; once any post of it was answered
    (or its thread resolved), its re-posts are not open work."""
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    items = []   # (key, url, answered, open)
    for th in (pr.get("reviewThreads") or {}).get("nodes") or []:
        cs = (th.get("comments") or {}).get("nodes") or []
        if cs and _is_bot(cs[0].get("author")):
            answered = bool(th.get("isResolved")) or not all(_is_bot(c.get("author")) for c in cs[1:])
            items.append((finding_key(cs[0], th.get("path")), cs[0].get("url") or "?", answered,
                          not answered and not th.get("isOutdated")))
    comments = (pr.get("comments") or {}).get("nodes") or []
    human_times = sorted(c.get("createdAt") or "" for c in comments if not _is_bot(c.get("author")))
    last_human = human_times[-1] if human_times else ""
    tops = [(c.get("createdAt") or "", c) for c in comments if _is_bot(c.get("author"))]
    tops += [(r.get("submittedAt") or "", r) for r in (pr.get("reviews") or {}).get("nodes") or []
             if _is_bot(r.get("author")) and r.get("state") == "CHANGES_REQUESTED"]
    items += [(finding_key(c), c.get("url") or "?", ts <= last_human, ts > last_human) for ts, c in tops]
    answered = {k for k, _, a, _ in items if a}
    out, seen = [], set()
    for k, u, _, live in items:
        if live and k not in answered and k not in seen:
            seen.add(k)
            out.append((k, u))
    return out


def bot_open(data: dict) -> list[str]:
    """URLs of the PR's open bot findings (bot_findings)."""
    return [u for _, u in bot_findings(data)]


def _check_findings(daemon, t: dict, pr: dict, sig: dict, findings: dict, data: dict | None) -> None:
    """Record an open PR's findings and tell the coordinator when they change: CI failing or bot
    review comments open is work (a fix task); CI green with every bot comment fixed or answered is
    when it may ask the user for a review. `data` is the PR's BOT_QUERY answer (None: unread)."""
    db, url = daemon.p.db, pr.get("url") or t["pr_url"]
    key = prguard.pr_key(url)
    if not key:
        return
    if sig["state"] != "OPEN":
        findings.pop(key, None)
        return
    old = findings.get(key) or {}
    if data is not None:
        found = bot_findings(data)
        bots, bot_keys = [u for _, u in found], sorted(k for k, _ in found)
    else:   # unread: keep what was known
        bots, bot_keys = old.get("bot", []), old.get("bot_keys", sorted(old.get("bot", [])))
    rec = {"task": t["id"], "url": url, "branch": pr.get("headRefName") or old.get("branch"),
           "failing": sig["failing"], "pending": sig["checks"] == "pending",
           "bot_open": len(bots), "bot": bots[:10], "bot_keys": bot_keys,
           "notified": old.get("notified"), "owner": old.get("owner")}
    findings[key] = rec
    if rec["pending"]:
        return
    dirty = bool(rec["failing"] or rec["bot_open"])
    what = "; ".join([*(["CI failing: " + ", ".join(rec["failing"])] if rec["failing"] else []),
                      *([f"{len(bots)} bot review comment(s) neither fixed nor answered: "
                         + ", ".join(bots[:5])] if bots else [])])
    # Open findings always have one owner: an open task on the PR, else a fix task the daemon queues
    # once the task that delivered it is done (Daemon.pr_findings_owner). pr-watch only reports them.
    owner, made = daemon.pr_findings_owner(key, rec, what) if dirty and hasattr(daemon, "pr_findings_owner") \
        else (None, False)
    lost = dirty and not owner and rec["owner"] is not None
    rec["owner"] = owner["id"] if owner else None
    # Keyed by finding, not URL: a bot re-posting the same open finding under a new URL is no news.
    mark = json.dumps([rec["failing"], bot_keys]) if dirty else "clean"
    if dirty and "bot_keys" not in old and rec["notified"] == json.dumps([rec["failing"], sorted(bots)]):
        rec["notified"] = mark   # a record from before keys were kept, already told these findings
    if not made and not lost and (mark == rec["notified"] or (not dirty and rec["notified"] is None)):
        rec["notified"] = mark   # a PR clean when first seen needs no news: its task's hand-off said so
        return
    rec["notified"] = mark
    status = "queued"
    if made:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. No open task owned them, so the daemon "
                f"queued code task #{owner['id']} on the PR's branch to fix or answer each. Queue no other task "
                f"for them. Do not ask the user to review it until pr-watch reports it clean.")
        status = "handled"
    elif owner:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. Open task #{owner['id']} "
                f"({owner['status']}) owns them"
                + (" (a review of its change; the daemon queues their fix once its review chain ends)"
                   if owner["kind"] == "review" else "")
                + ": queue no other task for them. Do not ask the user to review it until pr-watch reports it clean.")
        status = "queued" if owner["status"] == "blocked" else "handled"
    elif dirty:
        text = (f"PR for task #{t['id']} ({url}) has open findings: {what}. This is work"
                + (" and no open task owns them now" if lost else "")
                + ": queue a code task (on the PR's branch) to fix or answer each and get CI green. Do not ask "
                  "the user to review it until pr-watch reports it clean.")
    else:
        text = (f"PR for task #{t['id']} ({url}) is clean: CI green and every bot review comment fixed or "
                f"answered. If its review task passed, ask the user for a draft review now (ask_user, "
                f"blocking review, with its URL).")
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "pr", "pr_findings" if dirty else "pr_clean", "normal", text, status, t["id"]))

def watch_logs(daemon, payload: dict) -> str:
    db = daemon.p.db
    offsets = db.kv("log_offsets", {})
    n = 0
    for pattern in payload.get("files", []):
        for path in sorted(Path(daemon.p.root).glob(pattern)) if not os.path.isabs(pattern) else [Path(pattern)]:
            if not path.is_file():
                continue
            key = str(path)
            size = path.stat().st_size
            start = offsets.get(key, size)       # first sighting starts at the end: history is not news
            if size < start:
                start = 0                       # rotated or truncated
            if size > start:
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read(min(size - start, 256 * 1024)).decode(errors="replace")
                offsets[key] = start + len(chunk.encode())
                lines = [ln for ln in chunk.splitlines() if ln.strip()]
                interesting = [ln for ln in lines if daemon_rule(ln) != "info"]
                for ln in interesting[:20]:
                    daemon.observe(f"log:{path.name}", ln[:2000])
                    n += 1
            else:
                offsets[key] = size
    db.set_kv("log_offsets", offsets)
    return f"ok ({n} lines screened)"


def daemon_rule(line: str) -> str:
    from .screen import rule_severity
    return rule_severity(line)
