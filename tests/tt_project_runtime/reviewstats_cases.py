# Review stats and re-review fix-delta tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there). Add new review-routing tests here, not there.
from __future__ import annotations


def test_review_stats_areas_split_runtime_prompts_and_tests():
    from ttp import reviewstats
    assert reviewstats.area("plugins/x/runtime/ttp/daemon.py") == "runtime"
    assert reviewstats.area("plugins/x/skills/tt-x/SKILL.md") == "skills-prompts-config"
    assert reviewstats.area("plugins/x/template/prompts/worker.md") == "skills-prompts-config"
    assert reviewstats.area("plugins/x/.claude-plugin/plugin.json") == "skills-prompts-config"
    assert reviewstats.area("tests/test_x.py") == "tests-docs"
    assert reviewstats.area("tests/tt_project_runtime/push_cases.py") == "tests-docs"
    assert reviewstats.area("README.md") == "tests-docs"
    assert reviewstats.area(".changeset/a.md") == "tests-docs"
    assert reviewstats.areas({"a.py": 3, "README.md": 1, "tests/t.py": 2}) == ["runtime", "tests-docs"]
    assert reviewstats.areas(None) == []
    assert [reviewstats.size_bucket(n) for n in (None, 49, 50, 200, 201)] == ["unknown", "<50", "50-200",
                                                                             "50-200", ">200"]


def test_review_stats_table_is_added_to_an_existing_database(env):
    import sqlite3
    from ttp.db import DB
    path = env["tmp"] / "old.db"
    old = DB(path)
    tid = old.add_task("kept", "s", kind="review")
    old.conn.execute("DROP TABLE review_stats")   # a database from before the table
    old.conn.close()
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='review_stats'").fetchall()
    db = DB(path)
    assert db.task(tid)["title"] == "kept"
    assert db.q("SELECT * FROM review_stats") == []
    db.conn.close()
    assert DB(path).q("SELECT * FROM review_stats") == []   # opening again is a no-op


def test_a_re_review_leads_with_the_fix_delta_and_each_review_is_recorded(env):
    from ttp import audit, reviewstats, worktree
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40, "docs/notes.md": 2})
    failed = _git_out(p.root, "rev-parse", branch).strip()
    p.db.x("UPDATE tasks SET spent_usd=1.5 WHERE id=?", (rev["id"],))   # booked before the hand-off is read
    _fail_review(env, p, rev["id"], [{"title": "add the missing test", "spec": "test_x"}])
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (rev["id"],))
    assert row["head"] == failed and row["lines"] == 42 and row["areas"] == "runtime,tests-docs", row
    assert (row["rereview"], row["outcome"], row["cost_usd"], row["tier"]) == (0, "failed", 1.5, rev["tier"])
    assert reviewstats.failed_head(p.db, rev["id"]) == failed
    (fix,) = p.db.q("SELECT * FROM tasks WHERE kind='code' AND origin='daemon'")
    (re_rev,) = p.db.q("SELECT * FROM tasks WHERE kind='review' AND id!=?", (rev["id"],))
    fbranch = fix["branch"]
    # Queued with the fix: it names the failed head and where the new head will be, and leads with the delta.
    assert f"Fix delta: failed head {failed[:12]}, new head the tip of {fbranch} once #{fix['id']}" in re_rev["spec"]
    assert f"`git diff {failed[:12]}..{fbranch}`: check each finding above against it first" in re_rev["spec"]
    assert "full diff against the merge-base only where the delta touches it" in re_rev["spec"]
    path, _ = worktree.ensure(p, p.db.task(fix["id"]))
    (path / "test_x.py").write_text("def test_x(): pass\n")
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix")
    new = _git_out(path, "rev-parse", "HEAD").strip()
    p.db.update_task(fix["id"], status="running")
    run_dir = env["tmp"] / f"run-{fix['id']}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "fixed"}))
    Daemon(p.base)._finish_worker({"task": fix["id"]}, Usage(cost_usd=1.0), "ok", run_dir)
    # Once the fix is done: the new head and the delta's size, once per head.
    spec = p.db.task(re_rev["id"])["spec"]
    line = f"New head: {fbranch} at {new[:12]}. The fix delta {failed[:12]}..{new[:12]} changes 1 lines"
    assert spec.count(line) == 1, spec
    Daemon(p.base)._name_new_head(p.db.task(fix["id"]), p.db.task(re_rev["id"]))
    assert p.db.task(re_rev["id"])["spec"] == spec
    _fail_review(env, p, re_rev["id"], status="done", summary="each finding fixed")
    row = p.db.one("SELECT * FROM review_stats WHERE task=?", (re_rev["id"],))
    assert (row["head"], row["lines"], row["areas"]) == (new, 43, "runtime,tests-docs"), row
    assert (row["rereview"], row["outcome"]) == (1, "approved"), row
    # The efficiency audit reads them.
    line = next(x for x in audit.lines(p.db, p=p) if x.startswith("reviews finished: 2"))
    assert "re-review 1 (0 failed" in line and "first 1 (1 failed" in line and "<50 2 (1 failed" in line, line
    rep = audit.report(p.db, p=p)["reviews"]
    assert rep["n"] == 2 and {r["task"] for r in rep["rows"]} == {rev["id"], re_rev["id"]}


def test_a_coordinator_fix_review_leads_with_the_delta_from_the_recorded_failed_head(env):
    p = make(env)
    _, branch, _, (rev,) = _finish_code(env, p, "feature", {"app.py": 40})
    failed = _git_out(p.root, "rev-parse", branch).strip()
    _fail_review(env, p, rev["id"])   # no fix specs: the coordinator queues the fix
    _, fbranch, _, (rev2,) = _finish_code(env, p, "redo", {"app2.py": 3}, labels=[f"continues:{rev['id']}"])
    new = _git_out(p.root, "rev-parse", fbranch).strip()
    spec = p.db.task(rev2["id"])["spec"]
    assert f"Review #{rev['id']} failed on an earlier head. Fix delta: failed head {failed[:12]}, " \
           f"new head {new[:12]}. Lead with `git diff {failed[:12]}..{new[:12]}`: check each of its findings" in spec
    # No recorded head (a review from before the stats): the earlier guidance.
    p.db.x("DELETE FROM review_stats")
    _, _, _, (rev3,) = _finish_code(env, p, "redo again", {"app3.py": 3}, labels=[f"continues:{rev['id']}"])
    assert f"Review #{rev['id']} failed on an earlier head: check each of its findings is fixed" in \
        p.db.task(rev3["id"])["spec"]


