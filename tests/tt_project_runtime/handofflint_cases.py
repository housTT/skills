# The daemon's model-free hand-off lint (runtime/ttp/handofflint.py). Run from test_tt_project_runtime.py.


def _code_hand_off(p, env, tid, result):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-lint-{tid}-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return [e["text"] for e in p.db.q("SELECT text FROM events WHERE task=? AND kind='task_notes'", (tid,))]


def _own_push(p, value):
    cfg = json.loads(p.config_path.read_text())
    d = cfg.setdefault("delivery", {})
    if value is None:
        d.pop("own_push", None)
    else:
        d["own_push"] = value
    p.config_path.write_text(json.dumps(cfg))


def test_a_code_hand_off_proposing_to_push_its_unpublished_branch_is_flagged_without_a_manual_call(env):
    p = make(env)
    remote = _with_origin(env)
    _own_push(p, None)   # unset with draft PRs on: a code task may create its own branch
    tid, path, branch = _code_task(p, "work", status="running")
    _commit_file(path, "a")
    push_it = {"status": "done", "summary": "did it",
               "followups": [{"title": f"mechanical: push {branch} and open its draft PR", "spec": "push it"}]}
    (note,) = _code_hand_off(p, env, tid, push_it)
    assert "hand-off lint:" in note and branch in note and "ttp push --own-branch" in note
    # The flag is a note, so the daemon queues no continue hop for that follow-up by itself.
    assert not p.db.q("SELECT 1 FROM tasks WHERE labels LIKE ?", (f'%"continues:{tid}"%',))
    assert not p.db.q("SELECT 1 FROM events WHERE task=? AND text LIKE '%Continue hop #%'", (tid,))
    # Clean hand-offs pass: a follow-up that pushes nothing, and an upstream note naming the branch.
    tid2, path2, branch2 = _code_task(p, "work 2", status="running")
    _commit_file(path2, "b")
    assert _code_hand_off(p, env, tid2, {"status": "done", "summary": "ok", "followups": [
        {"title": "add docs", "spec": "describe the flag"},
        {"title": "upstream: push refusal", "spec": f"pushing {branch2} was refused"}]}) == []
    # Once the branch is on the remote, the same follow-up is not flagged.
    subprocess.run(["git", "-C", str(path), "push", "-q", str(remote), f"{branch}:refs/heads/{branch}"], check=True)
    tid3 = p.db.add_task("work 3", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid3, branch=branch)
    assert _code_hand_off(p, env, tid3, push_it) == []


def test_the_hand_off_lint_skips_projects_whose_code_tasks_cannot_publish_their_branch(env):
    from ttp import handofflint
    p = make(env)
    _with_origin(env)
    _own_push(p, "off")   # the review lands the branch: no own step to leave behind
    tid, path, branch = _code_task(p, "work", status="running")
    _commit_file(path, "a")
    fup = {"status": "done", "summary": "x", "followups": [{"title": f"mechanical: push {branch}", "spec": "y"}]}
    assert _code_hand_off(p, env, tid, fup) == []
    assert handofflint.applies({"delivery": {"own_push": "on"}})
    assert handofflint.applies({"delivery": {}})
    assert not handofflint.applies({"delivery": {"own_push": "existing_only"}})
    assert not handofflint.applies({"delivery": {"draft_prs": False}})
    # The matching rule itself: "push" plus the branch, or a mechanical follow-up; upstream notes never.
    assert handofflint.proposes_push({"followups": [{"title": "Push ttp/t1-x", "spec": ""}]}, "ttp/t1-x")
    assert handofflint.proposes_push({"followups": [{"title": "mechanical: open PR", "spec": "push it"}]}, "b")
    assert not handofflint.proposes_push({"followups": [{"title": "push something else", "spec": ""}]}, "b")
    assert not handofflint.proposes_push({"followups": [{"title": "upstream: push b", "spec": ""}]}, "b")
