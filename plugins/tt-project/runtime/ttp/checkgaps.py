# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One repair owner per missing check signature, with model-free retries on config changes."""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path

from . import push, worktree
from .db import dump_result, load_result
from .project import write_json

KEY = "check_applicability_gaps"
FAILURE = "check_failure.json"


def config_sig(cfg: dict) -> str:
    checks = (cfg.get("delivery") or {}).get("push_checks")
    return hashlib.sha256(json.dumps(checks, sort_keys=True).encode()).hexdigest()


def wait(p, task: dict, result: dict, run_dir: Path, cfg: dict) -> dict | None:
    """A check-only failure with tree evidence becomes a wait, never an approval. Unknown or
    related scope and mixed/code findings retain the ordinary failure path."""
    if result.get("review_failure") != "check_only" or result.get("status") not in ("failed", "blocked"):
        return None
    try:
        receipt = json.loads((run_dir / FAILURE).read_text())
        check = push.check_list([receipt["check"]])[0]
        path = Path(receipt["worktree"])
        head = receipt["head"]
        # Only this project's code worktrees; never act on another project's harness or state.
        if path.resolve() != p.root.resolve() and not path.resolve().is_relative_to(p.worktrees.resolve()):
            return None
        if check.config() not in [c.config() for c in push.check_list((cfg.get("delivery") or {}).get("push_checks"))]:
            return None
        if worktree._git(path, "rev-parse", "HEAD") != head:
            return None
        failure = push.target_missing(path, head, check, receipt["rc"], receipt["output"], receipt.get("base", ""))
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None
    if not failure or not failure["unrelated"]:
        return None
    sig, now = failure["sig"], time.time()
    key = "check:" + sig
    with p.db.tx():
        gaps = p.db.kv(KEY, {})
        gap = gaps.get(key)
        if not gap:
            owner = p.db.add_task(
                f"Repair check applicability {sig}",
                "Fix this project's own delivery.push_checks in its harness config. "
                f"Check signature {key}; command {str(check)!r}; missing targets: {', '.join(failure['targets'])}. "
                "Reproduce at the branch tip. Remove stale SHA pins or temporary environment paths and "
                "declare the actual scope with if_exists/if_changed, retaining mandatory coverage for "
                "related changes. Do not edit another project's harness or disable checks wholesale. "
                "Commit the config repair; the daemon reruns dependent checks without a model when it changes.",
                kind="harness", origin="daemon", labels=[key], priority=2)
            gap = {"owner": owner, "check": check.config(), "config": config_sig(cfg),
                   "created": now, "tasks": {}}
            gaps[key] = gap
        try:
            record = json.loads((run_dir / push.RECORDED).read_text())
            own = {str(c) for c in push.check_list((cfg.get("delivery") or {}).get("push_checks"))}
            extras = [str(c) for c in record.get("commands", []) if str(c) not in own]
        except (OSError, ValueError, TypeError, AttributeError):
            extras = []
        gap["tasks"][str(task["id"])] = {"run_dir": str(run_dir), "worktree": str(path), "head": head,
                                        "config": config_sig(cfg), "extras": extras}
        p.db.set_kv(KEY, gaps)
    return {**result, "status": "waiting", "summary": str(result.get("summary") or "") + f" Waiting on {key}, owner #{gap['owner']}.",
            "waiting_for": key, "check_gap": key, "retry_when": f"{push._own_ttp(p)} checks --gap {sig}",
            "retry_after_s": 1800, "wake_tier": "standard", "wait_kind": "external"}


def ready(p, sig: str) -> bool:
    """Read-only probe: a config change is the release condition, not an owner's claimed success."""
    gap = p.db.kv(KEY, {}).get("check:" + sig)
    return not gap or gap["config"] != config_sig(p.config())


def rerun(daemon) -> None:
    """Retarget each waiting hand-off to detached checks on its unchanged, clean tree. Config
    fingerprints are durable per task, so another tick or daemon restart does not duplicate work."""
    p, cfg = daemon.p, daemon.cfg
    gaps = p.db.kv(KEY, {})
    current = config_sig(cfg)
    for key, gap in gaps.items():
        for tid, member in gap["tasks"].items():
            task = p.db.task(int(tid))
            prev = load_result(task["result"]) if task else {}
            if not task or task["status"] != "queued" or prev.get("check_gap") != key or member["config"] == current:
                continue
            path, out = Path(member["worktree"]), Path(member["run_dir"])
            try:
                clean = (worktree._git(path, "rev-parse", "HEAD", check=False) == member["head"] and not
                         worktree._git(path, "status", "--porcelain", "--untracked-files=no", check=False))
            except (OSError, subprocess.SubprocessError):
                clean = False
            if not clean or prev.get("retry_when") != f"{push._own_ttp(p)} checks --gap {key.removeprefix('check:')}":
                # Changed/deleted worktrees wake normally; already-started checks keep their probe.
                continue
            # Save before starting: a restart sees the new probe and adopts the detached checks.
            probe = f"{push._own_ttp(p)} checks --result {shlex.quote(str(out))}"
            waiting = {**prev, "retry_when": probe, "waiting_for": "checks after applicability repair",
                       "wait_kind": "self"}
            write_json(out / "result.json", waiting)
            p.db.update_task(task["id"], result=dump_result(waiting), not_before=time.time() + 1800)
            member["config"] = current
            p.db.set_kv(KEY, gaps)
            extra = ["--fresh"] + (["--", *member["extras"]] if member.get("extras") else [])
            if not daemon._start_checks(path, out, f"check-gap-t{tid}", task["id"], extra):
                write_json(out / "result.json", prev)
                p.db.update_task(task["id"], result=dump_result(prev))
