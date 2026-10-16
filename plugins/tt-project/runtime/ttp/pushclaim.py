# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pushes a hand-off claims, checked on the remote before its task is recorded done.

A done hand-off that claims a push (a "pushed <sha> to <ref>" line in its summary, a `pushed` list in
result.json, or a detached `ttp push` its run started) leaves its task in `pushing` (daemon
_finish_worker). The daemon then asks the remote, model-free (verify_pushes): once every claimed
commit is the tip of its ref per `git ls-remote`, or reachable from it after a fetch, the hand-off is
recorded as usual. A ref or commit the remote definitely lacks fails the task with the remote, ref and
sha; a remote that cannot be read keeps it in `pushing` and asks again with backoff, up to BOUND_S,
after which it fails too. Push-queue landings are checked against the commit the queue recorded
(landed_sha), since its rebase gives commits new shas.

`ttp push-audit` lists done tasks of the last days whose claims are not on the remote, with the same
check; the daemon runs it every AUDIT_EVERY_S, raises one keyed observation per gap and puts one line
in the daily review. It also flags a done recorded from a checks-wait `on_pass` while the hand-off
named an on_pass_cmd that never ran (no on_pass/ folder in its run)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

KEY = "push_verify"              # tasks.result of a task in `pushing` until its claims are verified
HANDOFF = "push_claim_handoff.json"   # the hand-off waiting on the check, in its run's folder
AUDIT_KV = "push_audit"          # the last audit: {"at", "days", "counts", "gaps"}
CONFIRMED, MISSING, UNKNOWN = "confirmed", "missing", "unknown"
BACKOFF_S = (60, 120, 300, 600, 1200, 1800)
BOUND_S = 6 * 3600               # a claim the remote never answered for this long fails its task
LS_TIMEOUT_S, FETCH_TIMEOUT_S = 20, 40
PER_TICK = 4                     # tasks checked per daemon tick
AUDIT_EVERY_S, AUDIT_DAYS, AUDIT_REFS = 3600, 2, 20
ON_PASS_DIR = "on_pass"          # daemon.ON_PASS_DIR

_SHA = r"`?((?=[0-9a-f]*\d)[0-9a-f]{7,40})`?"
_REF = r"`?([A-Za-z0-9][\w./@+-]*\w)`?"
# "pushed abc1234 to dev", "landed as abc1234 on origin/dev", "pushed to dev as abc1234", "pushed tag v1 at abc1234"
_TAG = re.compile(rf"\bpushed\s+(?:the\s+)?tag\s+{_REF}\s+(?:at|to|on)\s+{_SHA}", re.I)
_REF_SHA = re.compile(rf"\b(?:pushed|landed)\s+(?:to|on|onto)\s+{_REF}\s+(?:as|at)\s+{_SHA}", re.I)
_SHA_REF = re.compile(rf"\b(?:pushed|landed)\s+(?:commit\s+)?(?:as\s+|at\s+)?{_SHA}(?:\s+(?:to|on|onto)\s+{_REF})?",
                      re.I)
_NEGATED = re.compile(r"(?:\bnot|\bnever|n't|\bbe|\bwill|\bwould|\bto)\s+$", re.I)
_NOT_REFS = {"the", "a", "an", "it", "its", "origin", "remote", "github", "branch", "upstream"}


def _git(repo, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 124, "", f"{type(e).__name__}: {e}")


def _commit(repo, rev: str) -> str:
    r = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _remotes(repo) -> list[str]:
    return _git(repo, "remote").stdout.split()


def _ref(repo, text: str, tag: bool = False) -> tuple[str, str] | None:
    """(remote, full ref) a claim names: `<remote>/<branch>`, a branch of origin, refs/heads/...,
    refs/tags/... or a tag. None when it names no remote of `repo`."""
    from .push import _split_target
    text = text.strip().strip("`").removeprefix("refs/remotes/")
    if text.startswith("refs/tags/"):
        text, tag = text[len("refs/tags/"):], True
    remotes = _remotes(repo)
    remote, name = _split_target(text, remotes)
    if remote not in remotes or not name:
        return None
    return remote, f"refs/tags/{name}" if tag else f"refs/heads/{name}"


def short_ref(remote: str, ref: str) -> str:
    return (f"{remote}/{ref[len('refs/heads/'):]}" if ref.startswith("refs/heads/")
            else f"tag {ref[len('refs/tags/'):]} on {remote}" if ref.startswith("refs/tags/") else f"{remote} {ref}")


def _default(p, repo) -> tuple[str, str] | None:
    """The project's push branch, where a claim that names no ref went."""
    from . import push
    try:
        remote, branch = push.target(p, Path(repo))
    except (ValueError, OSError):
        return None
    return (remote, f"refs/heads/{branch}") if remote in _remotes(repo) else None


def text_claims(p, text: str, repo=None) -> list[dict]:
    """The pushes a summary claims. Only commits of the project's repository count (a claim about
    another repository's commit cannot be checked here), and only remotes it has."""
    repo = repo or p.root
    out, seen = [], set()
    found = []
    for rx, order in ((_TAG, "ref_sha"), (_REF_SHA, "ref_sha"), (_SHA_REF, "sha_ref")):
        for m in rx.finditer(text or ""):
            if _NEGATED.search(text[max(0, m.start() - 12):m.start()]):
                continue
            ref, sha = (m.group(1), m.group(2)) if order == "ref_sha" else (m.group(2), m.group(1))
            if any(m.start() < e and s < m.end() for s, e in found):
                continue   # one phrase, one claim
            found.append((m.start(), m.end()))
            full = _commit(repo, sha)
            if not full:
                continue
            target = None
            if ref and ref.lower() not in _NOT_REFS:
                target = _ref(repo, ref, tag=rx is _TAG)
            target = target or _default(p, repo)
            if not target or (full, target) in seen:
                continue
            seen.add((full, target))
            out.append({"repo": str(repo), "remote": target[0], "ref": target[1], "sha": full,
                        "source": "its summary", "strict": False})
    return out


def _structured(p, result: dict) -> list[dict]:
    """A hand-off's own `pushed` list: [{"sha", "ref" | "branch" | "tag", "remote"?}]. Entries the push
    queue wrote (with a batch) are checked from its rows instead (queue_claims)."""
    raw = result.get("pushed")
    raw = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    out = []
    for e in raw:
        if not isinstance(e, dict) or e.get("batch") is not None or not isinstance(e.get("sha"), str):
            continue
        full = _commit(p.root, e["sha"].strip())
        name = e.get("tag") or e.get("ref") or e.get("branch")
        if not full:
            continue
        if isinstance(name, str) and name.strip():
            name = name.strip()
            if isinstance(e.get("remote"), str) and e["remote"].strip() and not name.startswith("refs/"):
                name = f"{e['remote'].strip()}/{name}"
            target = _ref(p.root, name, tag=bool(e.get("tag")))
        else:
            target = _default(p, p.root)
        if target:
            out.append({"repo": str(p.root), "remote": target[0], "ref": target[1], "sha": full,
                        "source": "its pushed list", "strict": True})
    return out


def _markers(p, tid: int) -> list[tuple[Path, dict]]:
    from . import push
    out = []
    for marker in sorted((p.state / push.DETACHED).glob(f"t{tid}-*.json")):
        m = push._read(marker)
        if isinstance(m, dict) and m.get("kind") != "batch":
            out.append((marker, m))
    return out


def marker_claims(p, tid: int, run_id) -> list[dict]:
    """The detached pushes a run started (and any of the task's still queued or running): each is a
    claim, checked once it wrote its outcome (_check_marker)."""
    out = []
    for marker, m in _markers(p, tid):
        mine = run_id is not None and str(m.get("run")) == str(run_id)
        if mine or m.get("status") in ("queued", "running"):
            out.append({"marker": marker.name, "source": f"its detached push {marker.stem}", "strict": True})
    pushed = {c["marker"] for c in out if _read_marker(p, c["marker"]).get("status") == "pushed"}
    # A push that failed and was then run again: the one that pushed is the claim.
    return [c for c in out if not pushed or c["marker"] in pushed
            or _read_marker(p, c["marker"]).get("status") in ("queued", "running")]


def queue_claims(p, tid: int) -> list[dict]:
    """The push queue's landings of task `tid`: the commit it recorded on its target."""
    out = []
    try:
        rows = p.db.q("SELECT branch, landed_sha, pushed_sha, target FROM push_queue WHERE task=? "
                      "AND status IN ('pushed','landed') ORDER BY id", (tid,))
    except Exception:
        return out
    for r in rows:
        sha = r["landed_sha"] or r["pushed_sha"]
        target = _ref(p.root, r["target"]) if r["target"] else None
        if sha and target:
            out.append({"repo": str(p.root), "remote": target[0], "ref": target[1], "sha": sha,
                        "source": "its push queue landing", "strict": True})
    return out


def claims(p, task: dict, result: dict, run: dict | None) -> list[dict]:
    """What a done hand-off claims was pushed: its summary's lines, its `pushed` list and its run's
    detached pushes. Local reads only (no network): it runs inside the run's finishing transaction."""
    if not isinstance(result, dict):
        return []
    found = marker_claims(p, task["id"], (run or {}).get("id")) + _structured(p, result)
    found += text_claims(p, str(result.get("summary") or ""))
    seen, out = set(), []
    for c in found:
        k = c.get("marker") or (c["sha"], c["remote"], c["ref"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _read_marker(p, name: str) -> dict:
    from . import push
    m = push._read(p.state / push.DETACHED / name)
    return m if isinstance(m, dict) else {}


def _ls(repo, remote: str, ref: str, cache: dict) -> tuple[list[str] | None, str]:
    """The shas `remote` has for `ref` (a tag's peeled commit last), None when it cannot be read."""
    k = (str(repo), remote, ref)
    if k not in cache:
        r = _git(repo, "ls-remote", remote, ref, timeout=LS_TIMEOUT_S)
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            cache[k] = (None, f"git ls-remote {remote} failed: {err[-1] if err else f'exit {r.returncode}'}")
        else:
            rows = [ln.split("\t") for ln in r.stdout.splitlines() if "\t" in ln]
            cache[k] = ([s for s, n in rows if n == ref] + [s for s, n in rows if n == f"{ref}^{{}}"], "")
    return cache[k]


def _fetch(repo, remote: str, ref: str, cache: dict) -> str:
    """The tip of remote's branch after a bounded fetch into its tracking ref, "" when it failed."""
    branch = ref[len("refs/heads/"):]
    k = ("fetch", str(repo), remote, ref)
    if k not in cache:
        r = _git(repo, "fetch", "-q", "--no-write-fetch-head", remote,
                 f"+{ref}:refs/remotes/{remote}/{branch}", timeout=FETCH_TIMEOUT_S)
        cache[k] = _commit(repo, f"refs/remotes/{remote}/{branch}") if r.returncode == 0 else ""
    return cache[k]


def verify(repo, remote: str, ref: str, sha: str, cache: dict | None = None) -> tuple[str, str]:
    """(state, why) of `sha` on `remote`'s `ref`: CONFIRMED when it is the tip per ls-remote or
    reachable from it after a fetch; MISSING when the remote has no such ref or the commit is not on
    it; UNKNOWN when the remote cannot be read (the network, a timeout)."""
    cache = {} if cache is None else cache
    where, s = short_ref(remote, ref), sha[:12]
    tips, err = _ls(repo, remote, ref, cache)
    if tips is None:
        return UNKNOWN, f"cannot check {s} on {where}: {err}"
    if not tips:
        return MISSING, f"{remote} has no {ref} ({s} claimed there)"
    tip = tips[-1]
    if tip.startswith(sha) or sha.startswith(tip):
        return CONFIRMED, f"{s} is the tip of {where}"
    if ref.startswith("refs/tags/"):
        return MISSING, f"{where} points at {tip[:12]}, not {s}"
    after = _fetch(repo, remote, ref, cache)
    if not after:
        return UNKNOWN, f"cannot fetch {where} to look for {s}"
    full = _commit(repo, sha)
    if not full:
        return MISSING, f"{s} is not on {where} (tip {tip[:12]}; no such commit after a fetch)"
    rc = _git(repo, "merge-base", "--is-ancestor", full, after).returncode
    if rc == 0:
        return CONFIRMED, f"{s} is on {where}"
    if rc == 1:
        return MISSING, f"{s} is not on {where} (tip {after[:12]})"
    return UNKNOWN, f"cannot tell whether {s} is on {where} (git merge-base exit {rc})"


def _check_marker(p, c: dict, cache: dict) -> tuple[str, str]:
    m = _read_marker(p, c["marker"])
    status, target = m.get("status"), str(m.get("target") or "")
    if status in ("queued", "running"):
        return UNKNOWN, f"{c['source']} is still {status}"
    repo = m.get("repo") or p.root
    to = _ref(repo, target) if target else None
    if not to:
        return MISSING, f"{c['source']} names no remote branch to check ({target or 'no target'})"
    sha = str(m.get("sha") or "") if status == "pushed" else ""
    if sha:
        return verify(repo, to[0], to[1], sha, cache)
    head = str(m.get("head") or "")
    why = str(m.get("reason") or m.get("message") or (f"exit {m['exit']}" if m.get("exit") is not None else "")
              or "no outcome")
    if head:
        state, said = verify(repo, to[0], to[1], head, cache)
        if state == CONFIRMED:
            return state, f"{said} (pushed another way after {c['source']} ended {status})"
        if state == UNKNOWN:
            return state, said
    return MISSING, (f"{c['source']} ended {status or 'without an outcome'} ({why[:200]}) and "
                     f"{(head or '?')[:12]} is not on {short_ref(*to)}")


def check(p, c: dict, cache: dict) -> tuple[str, str]:
    """One claim's (state, why); a summary's claim that names a ref the remote lacks also counts as
    pushed when its commit is on the project's push branch (the line may name a branch loosely)."""
    if c.get("marker"):
        return _check_marker(p, c, cache)
    repo = c.get("repo") or p.root
    state, why = verify(repo, c["remote"], c["ref"], c["sha"], cache)
    if state == MISSING and not c.get("strict") and (d := _default(p, repo)) and d != (c["remote"], c["ref"]):
        s2, w2 = verify(repo, d[0], d[1], c["sha"], cache)
        if s2 == CONFIRMED:
            return s2, f"{w2} ({why})"
    return state, why


def check_all(p, cs: list[dict], cache: dict | None = None) -> tuple[str, str, list[tuple[dict, str, str]]]:
    """(overall state, why, each claim's) of claims `cs`: MISSING if any is missing, else UNKNOWN if
    any cannot be told, else CONFIRMED."""
    cache = {} if cache is None else cache
    each = []
    for c in cs:
        try:
            state, why = check(p, c, cache)
        except Exception as e:   # an unreadable claim is not a verified one, nor a missing one
            state, why = UNKNOWN, f"could not check {c.get('source')}: {type(e).__name__}: {e}"
        each.append((c, state, why))
    for want in (MISSING, UNKNOWN):
        bad = [w for _, s, w in each if s == want]
        if bad:
            return want, "; ".join(bad), each
    return CONFIRMED, "; ".join(w for _, _, w in each), each


def delay_s(tries: int) -> float:
    return float(BACKOFF_S[min(max(tries, 0), len(BACKOFF_S) - 1)])


def describe(cs: list[dict]) -> str:
    parts = [c["source"] if c.get("marker") else f"{c['sha'][:12]} to {short_ref(c['remote'], c['ref'])}"
             for c in cs]
    return ", ".join(parts[:5]) + (f" and {len(parts) - 5} more" if len(parts) > 5 else "")


# The audit ------------------------------------------------------------------------------------------

def _last_run(db, tid: int):
    return db.one("SELECT * FROM runs WHERE task=? AND role='worker' ORDER BY id DESC LIMIT 1", (tid,))


def _on_pass_gap(prev: dict, run) -> str:
    """Why a done recorded from a waiting hand-off skipped the on_pass_cmd it named, else ""."""
    if not run or not run["dir"] or prev.get("run_status") != "model-free":
        return ""
    d = Path(run["dir"])
    try:
        waited = json.loads((d / "result.json").read_text())
    except (OSError, ValueError):
        return ""
    cmd = waited.get("on_pass_cmd") if isinstance(waited, dict) else None
    if isinstance(cmd, str) and cmd.strip() and not (d / ON_PASS_DIR).is_dir():
        return f"done recorded from its on_pass while its on_pass_cmd never ran: {cmd.strip()[:200]}"
    return ""


def audit(p, days: float = AUDIT_DAYS, now: float | None = None, max_refs: int = AUDIT_REFS) -> dict:
    """Done tasks of the last `days` whose claimed pushes are not on the remote: {"counts": {state: n},
    "gaps": [{"task", "title", "state", "why"}], "tasks": n}. Read-only, apart from fetching the
    remote's tracking refs. Past `max_refs` distinct refs the rest count as unknown."""
    from .db import load_result
    now = time.time() if now is None else now
    db = p.db
    rows = db.q("SELECT id, title, result FROM tasks WHERE status='done' AND updated>=? ORDER BY id",
                (now - days * 86400,))
    counts = {CONFIRMED: 0, MISSING: 0, UNKNOWN: 0}
    gaps: list[dict] = []
    cache: dict = {}
    refs: set = set()
    for t in rows:
        prev = load_result(t["result"])
        run = _last_run(db, t["id"])
        cs = queue_claims(p, t["id"])
        try:
            cs += [c for c in claims(p, {"id": t["id"]}, prev, dict(run) if run else None)
                   if c.get("marker") or not any(q["sha"].startswith(c["sha"][:12]) or c["sha"].startswith(q["sha"])
                                                 for q in cs)]
        except Exception as e:
            gaps.append({"task": t["id"], "title": t["title"], "state": UNKNOWN,
                         "why": f"could not read its claims: {type(e).__name__}: {e}"})
            counts[UNKNOWN] += 1
            continue
        for c in cs:
            k = c.get("marker") or (c["remote"], c["ref"])
            if k not in refs and len(refs) >= max_refs:
                state, why = UNKNOWN, f"not checked: more than {max_refs} refs in one audit"
            else:
                refs.add(k)
                _, state, why = check_all(p, [c], cache)[2][0]
            counts[state] += 1
            if state != CONFIRMED:
                gaps.append({"task": t["id"], "title": t["title"], "state": state, "why": why,
                             "sha": c.get("sha", ""), "source": c.get("source", "")})
        why = _on_pass_gap(prev, run)
        if why:
            counts[MISSING] += 1
            gaps.append({"task": t["id"], "title": t["title"], "state": MISSING, "why": why, "sha": "",
                         "source": "on_pass"})
    return {"at": now, "days": days, "tasks": len(rows), "counts": counts, "gaps": gaps}


def gap_key(g: dict) -> str:
    return f"push-gap:{g['task']}:{(g.get('sha') or g.get('source') or '')[:12]}"


def gap_text(g: dict) -> str:
    return (f"#{g['task']} {str(g.get('title') or '')[:80]} is done, but its claimed push is not on the remote: "
            f"{g['why']}. Requeue it to push, or record where its work landed.")


def summary_line(a: dict) -> str:
    c = a.get("counts") or {}
    days = a.get("days", AUDIT_DAYS)
    line = (f"{c.get(CONFIRMED, 0)} confirmed, {c.get(MISSING, 0)} missing, {c.get(UNKNOWN, 0)} unknown "
            f"across {a.get('tasks', 0)} done tasks of the last {days:g} d")
    missing = [g for g in a.get("gaps") or [] if g.get("state") == MISSING]
    if missing:
        line += "; missing: " + "; ".join(f"#{g['task']} ({g['why'][:160]})" for g in missing[:5])
        if len(missing) > 5:
            line += f" and {len(missing) - 5} more"
    return line


def daily_line(db) -> str:
    a = db.kv(AUDIT_KV)
    if not isinstance(a, dict) or not a.get("at"):
        return "not run yet"
    return summary_line(a)
