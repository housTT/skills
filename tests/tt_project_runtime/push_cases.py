# Push tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new push tests here, not there.
from __future__ import annotations


def test_merge3_near_refuses_additions_that_nearly_repeat_each_other():
    """With `near`, lines both sides added at one spot, or in different lines of one section (hunks),
    are not both kept when one nearly repeats the other: the same leading key, or most of the text
    alike. Additions that only look alike in shape (other keys, other words, new defs) still settle."""
    from ttp.batch import merge3
    base = "{\n  \"a\": 1,\n}\n"
    add = lambda x: base.replace("1,\n", "1,\n" + x)
    key_o, key_t = add('  "deep": "the analysis problem",\n'), add('  "deep": "a problem",\n')
    assert merge3(key_o, base, key_t) is not None                       # without near: kept both
    assert merge3(key_o, base, key_t, near=True) is None                # same key
    assert merge3(add("timeout = 5\n"), base, add("timeout = 10\n"), near=True) is None
    alike_o, alike_t = add("Never run the suite twice per fix.\n"), add("Never run the suite twice for a fix.\n")
    assert merge3(alike_o, base, alike_t, near=True) is None             # most of the text alike
    kinds: set = set()
    both = merge3(add('  "deep": "x",\n'), base, add('  "pr": "y",\n'), near=True, kinds=kinds)
    assert both == add('  "deep": "x",\n  "pr": "y",\n') and kinds == {"added"}
    assert merge3(add("Check the device first.\n"), base, add("Write the hand-off last.\n"), near=True)
    assert merge3("def test_one(env):\n    pass\n", "", "def test_two(env):\n    pass\n", py=True, near=True)
    # hunks: each side changes its own line of one section and adds a version of one key
    hb = "A\nB\nC\n"
    ho, ht = "A1\nkey: one way\nB\nC\n", "A\nB1\nkey: another way\nC\n"
    assert merge3(ho, hb, ht, hunks=True) == "A1\nkey: one way\nB1\nkey: another way\nC\n"
    assert merge3(ho, hb, ht, hunks=True, near=True) is None


def test_merge3_superseded_takes_theirs_only_when_it_holds_our_change():
    from ttp.batch import merge3
    base = "x = 1\nif a:\n    y = 2\nz = 3\n"
    comment_only = "x = 1  # set\nif a:\n    y = 2\nz = 3\n"
    assert merge3(comment_only, base, "x = 5\nif a:\n    y = 2\nz = 3\n", py=True) is None
    assert merge3(comment_only, base, "x = 5\nif a:\n    y = 2\nz = 3\n", py=True,
                  superseded=True) == "x = 5\nif a:\n    y = 2\nz = 3\n"
    ours = "x = 1\ny = 2\nz = 3\n"         # dropped the `if`
    theirs = "x = 1\n# no more if\ny = 2\nw = 4\nz = 3\n"
    assert merge3(ours, base, theirs, py=True, superseded=True) == theirs
    assert merge3("x = 1\ny = 9\nz = 3\n", base, theirs, py=True, superseded=True) is None
    # a "#" inside a string is code, not a comment
    assert merge3('x = "a #b"\nif a:\n    y = 2\nz = 3\n', base, "x = 5\nif a:\n    y = 2\nz = 3\n",
                  py=True, superseded=True) is None
    # in prompts only spacing is set aside
    assert merge3("A  rule.\nB\n", "A rule.\nB\n", "A rule, reworded.\nB\n", superseded=True) == \
        "A rule, reworded.\nB\n"
    assert merge3("A rule!\nB\n", "A rule.\nB\n", "A rule, reworded.\nB\n", superseded=True) is None


def test_merge3_superseded_keeps_a_deletion_upstream_only_reworded():
    """A line the project removed counts as removed upstream too only when upstream dropped it, not
    when it reworded it: otherwise the project's removal would be undone without anyone seeing it."""
    from ttp.batch import merge3
    flags = dict(taken_in=True, hunks=True, superseded=True)
    base = "a = 1\nb = 2\ncheck_dangerous()\nc = 3\n"
    assert merge3("a = 1\nb = 2\nc = 3\n", base, "a = 1\nb = 20\ncheck_dangerous(strict=True)\nc = 3\n",
                  py=True, **flags) is None
    # also when the project changed another line the same way upstream did
    assert merge3("a = 5\nb = 2\nc = 3\n", base, "a = 5\nb = 2\ncheck_dangerous(strict=True)\nc = 3\n",
                  py=True, **flags) is None
    md = "# R\n\n- one\n- rule two: do X\n- three\n"
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\n- one\n- rule two: always do X first\n- three\n",
                  **flags) is None
    # a deletion-only change where upstream rewrote the whole section is left for judgment
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\nAll new text.\nMore of it.\n", **flags) is None
    # a line upstream truly dropped still settles
    assert merge3("# R\n\n- one\n- three\n", md, "# R\n\n- one\n- three\n- four\n", **flags) == \
        "# R\n\n- one\n- three\n- four\n"


def test_merge3_superseded_reads_a_hash_inside_a_python_string_as_text():
    """Prompt text kept in a Python string holds Markdown headings: a project's edit of one is a
    change, not a comment, so it is not set aside as superseded."""
    from ttp.batch import merge3
    base = 'T = """\n# Rules\nbe nice\n"""\n'
    theirs = 'T = """\n# Rules\nbe kind\n"""\n'
    assert merge3('T = """\n# Rules (ours: never push)\nbe nice\n"""\n', base, theirs, py=True,
                  superseded=True) is None
    assert merge3('T = """\n# Rules\nbe nice  # ours\n"""\n', base, theirs, py=True,
                  superseded=True) is None
    # a real comment beside the string is still set aside
    assert merge3('T = """\n# Rules\nbe nice\n"""  # ours\n', base, theirs, py=True,
                  superseded=True) == theirs


def test_a_finished_worktree_stays_while_a_detached_push_runs_in_it(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import locks, push, worktree
    tid, path, _ = _code_task(p, "change")
    _commit_file(path, "change")
    marker = p.state / push.DETACHED / "t1-x.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"status": "running", "repo": str(path), "pid": 1}))
    held = locks.try_take([push._run_lock(marker)], "detached push t1-x")
    (res,) = worktree.sweep(p)
    assert path.exists() and res["held"] and "detached `ttp push`" in res["why"]
    held.close()     # the push ended (or died): the worktree goes as usual
    (res,) = worktree.sweep(p)
    assert not path.exists() and res["why"] is None


def test_local_only_clears_a_branch_recorded_as_superseded_by_a_commit_on_the_push_branch(env, monkeypatch, capsys):
    """A re-land after a push conflict puts a task's work on the push branch as one new commit with
    another patch and subject: the check flags it until a task records the commit that carries it.
    The record needs that commit on the push branch, covers the branch head it was made at, and also
    answers `landed:#<id>`."""
    from ttp import cli, landed
    from ttp import daemon as dm
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    _commit(path, "more.txt", "a\nb\nc\n")
    mine = _git_out(path, "rev-parse", "HEAD")
    (other / "more.txt").write_text("a\nb, settled\nc\n")
    _git_out(other, "add", "more.txt")
    _git_out(other, "commit", "-qm", "re-land of the more work after a push conflict")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    d = _local_only_daemon(p)
    _check_local_only(d)
    assert [e["task"] for e in _local_only_events(p)] == [tid]
    assert f"ttp task {p.name} superseded {tid} <sha>" in _local_only_events(p)[0]["text"]
    assert landed.check(p, repo, tid=tid)[0] == 1

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp_task("superseded", str(tid), mine) not in (0, None), "the branch's own head is not on the push branch"
    assert ttp_task("superseded", str(tid)) not in (0, None)
    assert p.db.q("SELECT * FROM task_superseded") == []
    assert ttp_task("superseded", str(tid), relanded[:12]) == 0
    assert f"recorded as superseded by {relanded[:10]}" in capsys.readouterr().out
    assert landed.superseded(p.db, [tid]) == {tid: [(mine, relanded)]}
    assert landed.check(p, repo, tid=tid)[0] == 0
    d._local_only_due = 0
    _check_local_only(d)
    assert p.db.kv(dm.KV_LOCAL_ONLY) is None
    _commit(path, "after.txt", "new work after the record\n")
    d._local_only_due = 0
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}, "work past the recorded head is flagged again"


def test_superseded_refuses_a_commit_that_cannot_carry_the_branchs_work(env, monkeypatch, capsys):
    """A record hides the branch for good at its head, so it must name a commit that can hold the
    work: not the branch's base or another commit the branch already has, not a later push-branch
    commit that changes none of the branch's files, and only for a done task."""
    from ttp import cli, landed
    from ttp import daemon as dm
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    _commit(path, "more.txt", "a\nb\n")
    _commit(path, "more.txt", "a\nb\nc\n")
    _commit(other, "unrelated.txt", "other work\n")
    unrelated = _git_out(other, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    base = _git_out(repo, "merge-base", branch, "origin/proj")
    d = _local_only_daemon(p)
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}

    def ttp_task(*args):
        try:
            cli.main(["task", p.name, *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp_task("superseded", str(tid), base) not in (0, None)
    assert "predates the work" in capsys.readouterr().err
    assert ttp_task("superseded", str(tid), unrelated) not in (0, None)
    assert "changes none of the files" in capsys.readouterr().err
    with pytest.raises(ValueError, match="not done"):
        p.db.update_task(tid, status="running")
        landed.record_superseded(p, tid, unrelated)
    p.db.update_task(tid, status="done")
    assert p.db.q("SELECT * FROM task_superseded") == []
    d._local_only_due = 0
    _check_local_only(d)
    assert set(p.db.kv(dm.KV_LOCAL_ONLY) or {}) == {str(tid)}, "the undelivered work stays flagged"


def test_a_task_whose_change_landed_under_another_commit_gets_no_review_or_fix(env, monkeypatch):
    """Replay of a duplicate: a code task's one commit (its change plus a version bump and changeset)
    sits in review while a fix chain of another task rebased the change and the push queue landed it
    as a new commit, without the bump and with other context around it, so git's patch-id differs. The
    sweep, the daemon's review and the fix of a failed review all find it landed: the task is recorded
    as superseded and closed, the coordinator is told once, and no review or fix is queued."""
    from ttp import landed
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    BUMP = {"files": ["VERSION"], "changeset_dir": ".changeset"}
    p.set_config("delivery.version_bump", BUMP)
    lines = [f"line {i}" for i in range(1, 11)]
    (repo / "VERSION").write_text("0.1.0\n")
    (repo / "app.txt").write_text("\n".join(lines) + "\n")
    _git_out(repo, "add", "VERSION", "app.txt")
    _git_out(repo, "commit", "-qm", "base")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    _git_out(other, "pull", "-q", "origin", "proj")
    tid, path, branch = _code_task(p, "duplicate change", status="review")
    p.db.update_task(tid, result=json.dumps({"status": "done", "summary": "made the change"}))
    change = lines[:5] + ["line 6 changed"] + lines[6:]
    (path / "app.txt").write_text("\n".join(change) + "\n")
    (path / "VERSION").write_text("0.1.1\n")
    (path / ".changeset").mkdir()
    (path / ".changeset" / "change.md").write_text("the change\n")
    _git_out(path, "add", "-A")
    _git_out(path, "commit", "-qm", "the change, with its version bump")
    head = _git_out(path, "rev-parse", "HEAD")
    assert landed.equivalent(p, branch, BUMP) is None, "not landed yet"
    # Another task's chain: a neighbouring change first, then the same change re-landed, then a fix.
    (other / "app.txt").write_text("\n".join(lines[:3] + ["line 4 by someone else"] + lines[4:]) + "\n")
    _git_out(other, "commit", "-qam", "a neighbouring change")
    (other / "app.txt").write_text("\n".join(lines[:3] + ["line 4 by someone else"] + change[4:]) + "\n")
    _git_out(other, "commit", "-qam", "re-land of the change under another task")
    relanded = _git_out(other, "rev-parse", "HEAD")
    _commit(other, "fix.txt", "a later fix of the change\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    tip = _git_out(repo, "rev-parse", "origin/proj")
    assert not landed.on_branch(repo, head, tip), "git's patch-id and the commit keys miss it"
    assert landed.equivalent(p, branch, None) is None, "the version files count unless excluded"
    assert landed.equivalent(p, branch, BUMP) == (relanded, head)

    def reset():
        p.db.x("DELETE FROM task_superseded")
        p.db.x("DELETE FROM events WHERE kind='task_superseded'")
        p.db.update_task(tid, status="review")

    def told():
        return p.db.q("SELECT status, text FROM events WHERE kind='task_superseded' AND task=?", (tid,))

    def closed():
        t = p.db.task(tid)
        assert t["status"] == "done" and json.loads(t["result"])["superseded_by"] == relanded
        assert landed.superseded(p.db, [tid]) == {tid: [(head, relanded)]}
        (e,) = told()
        assert e["status"] == "queued" and relanded[:10] in e["text"] and "no review or fix" in e["text"]
        assert not p.db.q("SELECT id FROM tasks WHERE id!=? AND status NOT IN ('done','failed','cancelled')",
                          (tid,)), "no review or fix queued"

    _ran(p, tid, time.time() - 1900)
    d = Daemon(p.base)
    _sweep(d)
    closed()
    _sweep(d)
    assert len(told()) == 1, "told once"
    reset()
    assert d._auto_review(p.db.task(tid), "made the change") is None
    closed()
    reset()
    review = p.db.add_task(f"Review #{tid}: duplicate change", "Review it.", kind="review", origin="coordinator",
                           depends_on=[tid])
    p.db.update_task(review, status="failed")
    assert d._fix_failed_review(p.db.task(review), [{"title": "fix it", "spec": "fix the finding"}], "bad") is None
    closed()


def test_backup_remote_refuses_to_name_a_branch(env):
    p = make(env)
    from ttp import coordinator as coord, push
    from ttp import daemon as dm
    from ttp.project import config_problems
    d = {"push_branch": "origin/team/work", "base_ref": "origin/dev"}
    for bad in ("main", "master", "origin/team/work", "team/work", "origin/dev", "dev"):
        assert "names a branch" in push.backup_problem({**d, "backup_remote": bad}) \
            or "not a git remote" in push.backup_problem({**d, "backup_remote": bad}), bad
        assert config_problems({"delivery": {**d, "backup_remote": bad}}), bad
    assert push.backup_problem({**d, "backup_remote": "backup"}) == ""
    assert push.backup_problem({**d, "backup_remote": ""}) == ""
    assert push.backup(env["repo"], "backup", "main")[0] == "refused"   # only ttp/t<id>-... branches
    p.set_config("delivery.base_ref", "dev")
    bad = {"type": "config_set", "key": "delivery.backup_remote", "value": "dev"}
    notes = coord.apply(p, [bad], user_turn=True)
    assert notes and "names a branch" in notes[0], notes
    assert "ask_user (blocking access)" in coord.apply(p, [{**bad, "value": "backup"}])[0]
    assert coord.apply(p, [{**bad, "value": "backup"}], user_turn=True) == []
    assert coord.apply(p, [{**bad, "value": ""}]) == []   # off needs no one's word
    # A key that names a branch anyway (edited by hand) pushes nothing and drops the queue.
    p.set_config("delivery.base_ref", "")
    remote = _backup_remote(env, "main")
    p.set_config("delivery.backup_remote", "main")
    tid, path, branch = _code_task(p, "work")
    _commit_file(path, "a")
    dm_ = dm.Daemon(p.base)
    dm_._queue_backup(p.db.task(tid))
    _finish_backup(dm_)
    assert p.db.kv(dm.KV_BACKUP) is None and not _remote_head(remote, branch)


def _ff_setup(env, monkeypatch, also):
    """_push_setup plus a `main` on origin at the target's tip, kept in step by delivery.fast_forward_also."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    if also is not None:
        p.set_config("delivery.fast_forward_also", also)
    return p, repo, origin, other


def test_push_fast_forwards_an_extra_branch_and_reads_it_back_from_the_remote(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["main"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(origin, "rev-parse", "main") == sha
    assert push.last_ff == [f"ff main {sha}"]
    # The detached marker records it, and the probe reports the remote's state.
    marker = p.state / "pushes" / "x.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": "x", "status": "pushed", "sha": sha, "target": "origin/proj",
                                  "fast_forward": [f"ff main {sha}", "not ff old: rejected"]}))
    capsys.readouterr()
    assert push.result(marker) == 0
    out = capsys.readouterr().out
    assert f"ttp push: ff main {sha}" in out and "ttp push: warning: not ff old: rejected" in out


def test_push_leaves_a_diverged_extra_branch_alone_and_says_not_ff(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["main"])
    _commit(other, "hotfix.txt", "fix\n")
    _git_out(other, "push", "-q", "origin", "HEAD:main")
    main = _git_out(origin, "rev-parse", "main")
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "a not-ff never fails the main push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert sha != main and _git_out(origin, "rev-parse", "main") == main, "the remote branch stays as it was"
    assert len(push.last_ff) == 1 and push.last_ff[0].startswith("not ff main: ") \
        and "not an ancestor" in push.last_ff[0], push.last_ff
    assert push.ff_warnings(push.last_ff) == push.last_ff


def test_push_without_fast_forward_also_moves_nothing_else(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _ff_setup(env, monkeypatch, None)
    main = _git_out(origin, "rev-parse", "main")
    for also in (None, [], ""):
        if also is not None:
            p.set_config("delivery.fast_forward_also", also)
        _commit(repo, "mine.txt", f"{also!r}\n")
        assert push.run(p, repo) == 0
        assert _git_out(origin, "rev-parse", "main") == main and push.last_ff == []


def test_fast_forward_also_scalar_is_reported_and_never_raises_after_a_push(env, monkeypatch):
    from ttp import push
    from ttp.project import config_problems
    for bad in (True, 5):
        with pytest.raises(ValueError):
            push.fast_forward_check(bad, "origin/proj")
        assert any("fast_forward_also" in l for l in
                   config_problems({"delivery": {"push_branch": "origin/proj", "fast_forward_also": bad}})), bad
        assert push.fast_forward_list(bad) == [] and push.exclude_list(bad) == []
    p, repo, origin, other = _ff_setup(env, monkeypatch, None)
    main = _git_out(origin, "rev-parse", "main")
    for bad in (True, 5):
        p.set_config("delivery.fast_forward_also", bad)
        _commit(repo, "mine.txt", f"{bad!r}\n")
        assert push.run(p, repo) == 0
        assert _git_out(origin, "rev-parse", "main") == main and push.last_ff == []


def test_scalar_push_checks_after_push_and_exclude_paths_are_reported_and_refused(env, monkeypatch, capsys):
    from ttp import push
    from ttp.coordinator import apply
    from ttp.project import config_problems
    p = make(env)
    for bad in (True, 5, 1.5):
        for key in ("push_checks", "after_push", "push_exclude_paths", "mirror_remotes"):
            assert config_problems({"delivery": {key: bad}}) == [f"delivery.{key}: {bad!r}: not a list"], (key, bad)
        with pytest.raises(ValueError, match="not a list"):
            push.checks_of(bad)
        probs = apply(p, [{"type": "config_set", "key": f"delivery.{k}", "value": bad}
                          for k in ("push_checks", "after_push")])
        assert len(probs) == 2 and all("not a list" in x for x in probs), probs
        assert "push_checks" not in (p.config().get("delivery") or {})
        probs = apply(p, [{"type": "config_set", "key": "delivery.mirror_remotes", "value": bad}])
        assert probs == [f"delivery.mirror_remotes: {bad!r}: not a list"] or "not a list" in str(probs), probs
        assert "mirror_remotes" not in (p.config().get("delivery") or {})
    for ok in (None, False, 0, "", [], "tmp", ["tmp"]):   # off or a list: no problem
        assert config_problems({"delivery": {"push_exclude_paths": ok}}) == [], ok
    # A scalar push_exclude_paths would turn the guard off: the push branch refuses, --own does not.
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_exclude_paths", 5)
    (repo / "tmp").mkdir()
    _git_out(repo, "checkout", "-q", "-b", "ttp/t31-exp")
    _commit(repo, "tmp/scratch.txt", "x\n")
    before = _git_out(origin, "rev-parse", "proj")
    assert _ttp_push() == 2 and "delivery.push_exclude_paths: 5: not a list" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before
    monkeypatch.setenv("TTP_TASK", "31")
    assert _ttp("push", "--own") == 0


def test_an_ordinary_push_runs_after_push_at_the_pushed_commit_and_the_daemon_reports_it(env, monkeypatch):
    """after_push runs after every guarded `ttp push`, not only push queue batches: in a clean checkout
    at the pushed commit, with a durable receipt the daemon turns into one event."""
    from ttp import push, pushq
    seen = env["tmp"] / "after_push.txt"
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [f"(env | grep '^TTP_PUSH'; echo head $(git rev-parse HEAD); pwd) > {seen}"])
    monkeypatch.setenv("TTP_TASK", "7")
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    lines = seen.read_text().splitlines()
    for want in (f"TTP_PUSHED_SHA={sha}", "TTP_PUSH_TARGET=origin/proj", "TTP_PUSH_TASKS=7", f"head {sha}"):
        assert want in lines, (want, lines)
    assert not any(x.startswith("TTP_PUSH_BATCH=") for x in lines), lines
    (r,) = _receipts(p)
    assert f"TTP_PUSH_RECEIPT={p.state / 'after_push' / (r['id'] + '.json')}" in lines, lines
    assert r["status"] == "finished" and r["after_push"]["status"] == "ok" and r["pushed_sha"] == sha
    assert push.last_after["status"] == "ok" and not any(p.worktrees.glob("after_push-*"))
    assert not list((p.state / "locks").glob("after_push:*")), "a finished after_push leaves no lock"
    alerts = []
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [r["id"]] and alerts == []
    ev = p.db.one("SELECT * FROM events WHERE kind='after_push_ok'")
    assert ev and sha[:7] in ev["text"] and ev["status"] == "handled"
    assert pushq.tend_receipts(p, lambda *a, **k: alerts.append(a)) == [], "reported once"
    assert p.db.kv(pushq.AFTER_PUSH_OK)


def test_after_push_does_not_run_on_a_refused_push_an_own_branch_push_or_when_unset(env, monkeypatch):
    from ttp import push
    ran = env["tmp"] / "ran"
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f bad.txt"])
    p.set_config("delivery.after_push", [f"touch {ran}"])
    _commit(repo, "bad.txt", "bad\n")
    assert push.run(p, repo) == push.CHECKS_FAILED
    assert not ran.exists() and _receipts(p) == [] and push.last_after is None
    _git_out(repo, "rm", "-q", "bad.txt")
    _git_out(repo, "commit", "-qm", "fix")
    monkeypatch.setenv("TTP_TASK", "40")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t40-feature")
    _commit(repo, "a.txt", "a\n")
    assert push.run(p, repo, own=True) == 0 and _git_out(origin, "rev-parse", "ttp/t40-feature")
    assert not ran.exists() and _receipts(p) == [], "an own-branch push never deploys"
    _git_out(repo, "checkout", "-q", "-B", "work", "HEAD")
    for unset in (None, [], ""):
        p.set_config("delivery.after_push", unset)
        _commit(repo, "b.txt", f"{unset!r}\n")
        assert push.run(p, repo) == 0
        assert not ran.exists() and _receipts(p) == [] and push.last_after is None


def test_a_failed_or_timed_out_after_push_keeps_the_push_and_alerts_once(env, monkeypatch, capsys):
    from ttp import alerts as alerts_mod, push, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", ["echo deploying; echo deploy-broke; exit 3"])
    _commit(repo, "mine.txt", "mine\n")
    capsys.readouterr()
    assert push.run(p, repo) == 0, "a failed after_push never fails the push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert sha == _git_out(repo, "rev-parse", "HEAD")
    assert "warning: after_push failed (exit 3)" in capsys.readouterr().err
    (r,) = _receipts(p)
    assert (r["after_push"]["status"], r["after_push"]["exit"]) == ("failed", 3) and r["pushed_sha"] == sha
    raised = []
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 1 and raised[0][0] == "after_push_failed" and "exit 3" in raised[0][1] \
        and raised[0][2]["severity"] == "high", raised
    ev = p.db.one("SELECT * FROM events WHERE kind='after_push_failed'")
    assert ev["status"] == "queued" and "deploy-broke" in ev["text"] and "The push stands" in ev["text"]
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 1, "each receipt is reported once"
    # The alert lasts until an after_push succeeds.
    since = time.time() - 1
    assert alerts_mod.holds(p.db, "after_push_failed", since, time.time())
    # Timeout: the command is killed at after_push_timeout_s; the push still stands.
    p.set_config("delivery.after_push", ["sleep 60"])
    p.set_config("delivery.after_push_timeout_s", 1)
    _commit(repo, "two.txt", "two\n")
    t0 = time.time()
    assert push.run(p, repo) == 0 and time.time() - t0 < 30
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_after["status"] == "timeout"
    pushq.tend_receipts(p, lambda key, text, **k: raised.append((key, text, k)))
    assert len(raised) == 2 and "timed out" in raised[1][1]
    p.set_config("delivery.after_push", ["true"])
    p.set_config("delivery.after_push_timeout_s", 120)   # 1 s is too short for `true` on a loaded host
    _commit(repo, "three.txt", "three\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "ok"
    pushq.tend_receipts(p)
    assert not alerts_mod.holds(p.db, "after_push_failed", since, time.time()), "a success clears it"


def test_an_after_push_whose_process_died_is_reported_as_killed(env, monkeypatch):
    from ttp import pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    folder = p.state / "after_push"
    folder.mkdir(parents=True)
    (folder / "p1.json").write_text(json.dumps({"id": "p1", "status": "running", "pushed_sha": "a" * 40,
                                                "target": "origin/proj", "started": time.time()}))
    raised = []
    assert pushq.tend_receipts(p, lambda key, text, **k: raised.append(text)) == ["p1"]
    assert len(raised) == 1 and "stopped before it finished" in raised[0]
    assert json.loads((folder / "p1.json").read_text())["after_push"]["status"] == "killed"


def test_a_newer_ok_receipt_clears_an_after_push_failure_an_older_one_raised_in_the_same_tick(env, monkeypatch):
    """The tick's `now` is earlier than the alert an older failed receipt raises in that tick: the
    success a newer receipt records must still count as after it."""
    from ttp import alerts as alerts_mod, pushq
    p = make(env)
    folder = p.state / "after_push"
    folder.mkdir(parents=True)
    for rid, status in (("p1", "failed"), ("p2", "ok")):
        (folder / f"{rid}.json").write_text(json.dumps({
            "id": rid, "status": "finished", "pushed_sha": "a" * 40, "target": "origin/proj",
            "after_push": {"status": status, "exit": 3 if status == "failed" else 0}}))
    raised = []
    assert pushq.tend_receipts(p, lambda key, text, **k: raised.append(time.time()),
                               now=time.time() - 5) == ["p1", "p2"]
    assert len(raised) == 1
    assert not alerts_mod.holds(p.db, "after_push_failed", raised[0], time.time()), "the newer success clears it"


def test_an_ordinary_push_gives_after_push_the_version_at_the_pushed_commit(env, monkeypatch):
    from ttp import push
    seen = env["tmp"] / "version.txt"
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", [f"echo \"v=$TTP_PUSHED_VERSION\" > {seen}"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "ok"
    assert seen.read_text().strip() == "v=0.1.0"
    assert _receipts(p)[0]["version"] == "0.1.0"
    p.set_config("delivery.version_bump", None)   # no version_bump: no version to give
    _commit(repo, "two.txt", "two\n")
    assert push.run(p, repo) == 0
    assert seen.read_text().strip() == "v="


def test_a_malformed_after_push_is_recorded_as_a_failure_that_alerts(env, monkeypatch, capsys):
    from ttp import push, pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.after_push", 5)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "the push stands"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_after["status"] == "failed" and "not a list" in push.last_after["tail"]
    (r,) = _receipts(p)
    assert r["status"] == "finished" and r["after_push"]["status"] == "failed"
    raised = []
    pushq.tend_receipts(p, lambda key, text, **k: raised.append(key))
    assert raised == ["after_push_failed"]
    assert pushq._after_push_set(p, None), "a malformed after_push is not off: its failure stands"
    # A value that makes reading the commands raise is recorded the same way.
    p.set_config("delivery.after_push", ["./deploy"])
    real = push.check_list

    def check_list(v):
        if v == ["./deploy"]:
            raise ValueError("bad entry")
        return real(v)
    monkeypatch.setattr(push, "check_list", check_list)
    _commit(repo, "two.txt", "two\n")
    assert push.run(p, repo) == 0 and push.last_after["status"] == "failed"
    pushq.tend_receipts(p, lambda key, text, **k: raised.append(key))
    assert raised == ["after_push_failed"] * 2 and "bad entry" in max(_receipts(p), key=lambda r: r["started"])["after_push"]["tail"]


def test_fast_forward_also_refuses_the_push_branch_and_bad_names(env, monkeypatch):
    from ttp import push
    from ttp import coordinator as coord
    from ttp.project import config_problems
    p, repo, origin, other = _ff_setup(env, monkeypatch, ["proj", "a..b", "main"])
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert push.last_ff == ["not ff proj: it is the push branch itself (delivery.push_branch)",
                            "not ff a..b: not a valid branch name", f"ff main {sha}"]
    for bad in ("proj", "origin/proj", "a..b", "-x"):
        with pytest.raises(ValueError):
            push.fast_forward_check([bad], "origin/proj")
        assert config_problems({"delivery": {"push_branch": "origin/proj", "fast_forward_also": [bad]}}), bad
    assert push.fast_forward_check('["main", "main", " release "]', "origin/proj") == ["main", "release"]
    # From chat it needs the user's word; turning it off does not.
    bad = {"type": "config_set", "key": "delivery.fast_forward_also", "value": ["main"]}
    assert "ask_user (blocking restriction)" in coord.apply(p, [bad])[0]
    assert "push branch" in coord.apply(p, [{**bad, "value": ["proj"]}], user_turn=True)[0]
    assert coord.apply(p, [bad], user_turn=True) == [] and p.config()["delivery"]["fast_forward_also"] == ["main"]
    assert coord.apply(p, [{**bad, "value": []}]) == []



def _mirror_setup(env, monkeypatch, mirrors=None, branch="dev"):
    """_push_setup plus a second bare repository, git remote `mirror`, whose `branch` is at the
    target's tip, kept in step by delivery.mirror_remotes."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    mirror = env["tmp"] / "mirror.git"
    subprocess.run(["git", "init", "-q", "--bare", str(mirror)], check=True)
    _git_out(repo, "remote", "add", "mirror", str(mirror))
    _git_out(repo, "push", "-q", "mirror", f"HEAD:refs/heads/{branch}")
    p.set_config("delivery.mirror_remotes", mirrors if mirrors is not None else [{"remote": "mirror", "branch": branch}])
    return p, repo, origin, mirror


def _no_force(monkeypatch):
    """Every git call mirror() makes, recorded; none may force."""
    from ttp import push
    calls, real = [], push._git_net
    monkeypatch.setattr(push, "_git_net", lambda repo, *a, **k: (calls.append(a), real(repo, *a, **k))[1])
    forced = lambda: [a for a in calls if a[0] == "push" and (  # noqa: E731
        {"-f", "--force", "--force-with-lease", "--mirror"} & set(a) or any(x.startswith("+") for x in a[1:]))]
    return calls, forced


def test_push_mirrors_the_pushed_head_to_another_remote_fast_forward_only(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch, [{"remote": "mirror", "branch": "dev"}, "mirror/new"])
    calls, forced = _no_force(monkeypatch)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(mirror, "rev-parse", "dev") == sha and _git_out(mirror, "rev-parse", "new") == sha, \
        "the same commit goes to the mirror; a branch it lacks is created"
    assert push.last_ff == [f"mirror mirror/dev {sha}", f"mirror mirror/new {sha}"]
    assert any(a[0] == "fetch" for a in calls), "each mirror is fetched first"
    assert [a for a in calls if a[0] == "push"] and forced() == [], calls
    assert f"ttp push: mirror mirror/dev {sha}" in capsys.readouterr().err
    # Already there: nothing is pushed, and it still reads back as mirrored.
    calls.clear()
    assert push.mirror(repo, sha, push.mirror_list([{"remote": "mirror", "branch": "dev"}]), "origin/proj") == \
        [f"mirror mirror/dev {sha}"] and not [a for a in calls if a[0] == "push"]


def test_push_skips_a_diverged_mirror_names_both_commits_and_notes_it(env, monkeypatch):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    side = env["tmp"] / "side"
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(mirror), str(side)], check=True)
    _commit(side, "hotfix.txt", "fix\n")
    _git_out(side, "push", "-q", "origin", "HEAD:dev")
    theirs = _git_out(mirror, "rev-parse", "dev")
    calls, forced = _no_force(monkeypatch)
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0, "a diverged mirror never fails the main push"
    sha = _git_out(origin, "rev-parse", "proj")
    assert _git_out(mirror, "rev-parse", "dev") == theirs, "the mirror stays as it was"
    assert not [a for a in calls if a[0] == "push"] and forced() == [], "nothing is pushed to a diverged mirror"
    [line] = push.last_ff
    assert line.startswith("not mirrored mirror/dev: ") and theirs in line and sha in line, line
    assert push.ff_warnings(push.last_ff) == push.last_ff
    note = (run_dir / "progress.md").read_text()
    assert "not mirrored mirror/dev" in note and theirs in note and sha in note, note


def test_a_mirror_that_fails_never_fails_the_main_push(env, monkeypatch):
    from ttp import push
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch, [{"remote": "gone", "branch": "dev"},
                                                                {"remote": "mirror", "branch": "dev"},
                                                                {"remote": "nowhere", "branch": "dev"}])
    _git_out(repo, "remote", "add", "gone", str(env["tmp"] / "no-such-repo.git"))
    _commit(repo, "mine.txt", "mine\n")
    assert push.run(p, repo) == 0
    sha = _git_out(origin, "rev-parse", "proj")
    assert push.last_ff[0].startswith("not mirrored gone/dev: gone cannot be read"), push.last_ff
    assert push.last_ff[1:] == [f"mirror mirror/dev {sha}", "not mirrored nowhere/dev: there is no git remote named nowhere"]
    # mirror() itself raising: the push stands and each mirror says why.
    monkeypatch.setattr(push, "mirror", lambda *a, **k: 1 / 0)
    _commit(repo, "more.txt", "more\n")
    assert push.run(p, repo) == 0 and _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert push.last_ff[0] == "not mirrored gone/dev: ZeroDivisionError: division by zero", push.last_ff
    # Off by default and when emptied: nothing else moves.
    for off in ([], ""):
        p.set_config("delivery.mirror_remotes", off)
        _commit(repo, f"off{off!r}.txt", "x\n")
        assert push.run(p, repo) == 0 and push.last_ff == []


def test_mirror_remotes_refuses_the_push_branch_and_bad_entries(env, monkeypatch):
    from ttp import push
    from ttp import coordinator as coord
    from ttp.project import config_problems
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    for bad in ([{"remote": "origin", "branch": "proj"}], ["origin/proj"], [{"remote": "mirror", "branch": "a..b"}],
                [{"remote": "-x", "branch": "dev"}], [{"branch": "dev"}], ["dev"], [{"remote": "mirror", "branch": ""}]):
        with pytest.raises(ValueError):
            push.mirror_check(bad, "origin/proj")
        assert config_problems({"delivery": {"push_branch": "origin/proj", "mirror_remotes": bad}}), bad
    assert push.mirror_check('[{"remote": "mirror", "branch": "dev"}, " mirror/dev ", "m2/a/b"]', "origin/proj") == \
        [{"remote": "mirror", "branch": "dev"}, {"remote": "m2", "branch": "a/b"}]
    assert push.mirror(repo, "ab" * 20, [{"remote": "origin", "branch": "proj"}], "origin/proj") == \
        ["not mirrored origin/proj: it is the push branch itself (delivery.push_branch)"]
    # From chat it needs the user's word; turning it off does not.
    ask = {"type": "config_set", "key": "delivery.mirror_remotes", "value": [{"remote": "mirror", "branch": "dev"}]}
    assert "ask_user (blocking restriction)" in coord.apply(p, [ask])[0]
    assert "push branch" in coord.apply(p, [{**ask, "value": ["origin/proj"]}], user_turn=True)[0]
    assert coord.apply(p, [ask], user_turn=True) == []
    assert p.config()["delivery"]["mirror_remotes"] == [{"remote": "mirror", "branch": "dev"}]
    assert coord.apply(p, [{**ask, "value": []}]) == []


def _plugin_commit(path, version, name, text):
    """Commit a file of plugin `p` and set both its manifests to `version`."""
    for m in (".claude-plugin", ".codex-plugin"):
        (path / "plugins" / "p" / m).mkdir(parents=True, exist_ok=True)
        (path / "plugins" / "p" / m / "plugin.json").write_text(json.dumps({"name": "p", "version": version}))
    (path / "plugins" / "p" / name).write_text(text)
    _git_out(path, "add", "plugins")
    _git_out(path, "commit", "-qm", f"p {version}")


def test_push_refuses_a_rebased_change_that_keeps_the_targets_plugin_version(env, monkeypatch, capsys):
    """Two batches both bumped 0.1.0 to 0.1.1: the second rebases cleanly onto the first, but it
    must not land as 0.1.1 too, or an upgrade to strictly newer versions skips it. The guard is
    `delivery.plugin_version_guard`, off by default: not every project's repository holds plugins."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp.project import config_problems
    assert any("plugin_version_guard" in x for x in config_problems({"delivery": {"plugin_version_guard": "yes"}}))
    assert not any("plugin_version_guard" in x for x in config_problems({"delivery": {"plugin_version_guard": True}}))
    from ttp import push as pushmod
    assert [pushmod.version_guard(d) for d in ({}, None, {"plugin_version_guard": "yes"},
                                              {"plugin_version_guard": True})] == [False, False, False, True]
    p.set_config("delivery.plugin_version_guard", True)
    _plugin_commit(repo, "0.1.0", "base.txt", "base\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    _git_out(other, "pull", "-q", "origin", "proj")
    _plugin_commit(other, "0.1.1", "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    moved = _git_out(origin, "rev-parse", "proj")
    _plugin_commit(repo, "0.1.1", "mine.txt", "mine\n")
    assert _ttp_push() == 4
    assert "plugins/p 0.1.1" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == moved, "a same-version batch was pushed"

    _plugin_commit(repo, "0.1.2", "mine.txt", "mine\n")
    assert _ttp_push() == 0, "bumped past the target's version, it goes through"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")

    _commit(repo, "outside.txt", "not a plugin\n")
    assert _ttp_push() == 0, "a change outside plugins/ needs no bump"

    p.set_config("delivery.plugin_version_guard", False)
    _plugin_commit(repo, "0.1.2", "more.txt", "more\n")
    assert _ttp_push() == 0, "with the guard off, a plugin change that keeps the version goes through"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")


def test_push_to_main_needs_allow_protected_push_branch_and_keeps_checks_rebase_and_no_force(env, monkeypatch, capsys):
    # main (here also the remote's default branch) is refused by default, with a message that names
    # the opt-in and forbids a raw `git push`. With delivery.allow_protected_push_branch on, the push
    # goes through the same checks and rebase as any other branch; a detached HEAD never does.
    from ttp import coordinator as coord, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f broken.txt"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    before = _git_out(origin, "rev-parse", "main")
    p.set_config("delivery.push_branch", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 2
    err = capsys.readouterr().err
    assert "refusing to push to origin/main" in err and "delivery.allow_protected_push_branch" in err \
        and "never fall back to a raw `git push`" in err, err
    for off in (None, False, "false", "", 0):
        assert push.allow_protected({"allow_protected_push_branch": off}) is False, off
    assert push.allow_protected(None) is False
    # From chat it needs the user's word; turning it off does not.
    ask = {"type": "config_set", "key": "delivery.allow_protected_push_branch", "value": True}
    assert "ask_user (blocking restriction)" in coord.apply(p, [ask])[0]
    assert "allow_protected_push_branch" not in (p.config().get("delivery") or {})
    assert coord.apply(p, [{**ask, "value": False}]) == []
    assert coord.apply(p, [ask], user_turn=True) == [] and push.allow_protected(p.config()["delivery"])
    # Checks still run and still stop it.
    _commit(repo, "broken.txt", "x\n")
    assert _ttp_push() == 4 and _git_out(origin, "rev-parse", "main") == before
    _git_out(repo, "reset", "-q", "--hard", "HEAD~1")
    # Someone else moved main: the push rebases onto it, never forces.
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:main")
    moved = _git_out(origin, "rev-parse", "main")
    assert _ttp_push() == 0
    assert _git_out(origin, "rev-parse", "main") == _git_out(repo, "rev-parse", "HEAD") != moved
    assert _git_out(origin, "rev-parse", "main~1") == moved
    assert _git_out(origin, "show", "main:theirs.txt") == "theirs" and _git_out(origin, "show", "main:mine.txt") == "mine"
    # The opt-in never covers a detached HEAD, nor `--own` publishing to main.
    p.set_config("delivery.push_branch", "origin/HEAD")
    assert _ttp_push() == 2 and "refusing to push to origin/HEAD" in capsys.readouterr().err
    p.set_config("delivery.push_branch", "origin/proj")
    _git_out(repo, "checkout", "-q", "-B", "main")
    with pytest.raises(ValueError, match="shared branch"):
        push.own_target(p, repo)


def test_a_push_batch_and_the_queue_publish_to_main_only_with_allow_protected_push_branch(env, monkeypatch):
    from ttp import pushq
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f broken.txt"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    p.set_config("delivery.push_branch", "origin/main")
    before = _git_out(origin, "rev-parse", "main")
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    assert "allow_protected_push_branch" in pushq._refusal(p, "origin", "main")
    rc, m = _run_batch(p, _batch_marker(p, heads, target="origin/main"), monkeypatch)
    assert rc == 2 and _statuses(m) == ["refused"] and _git_out(origin, "rev-parse", "main") == before, m
    p.set_config("delivery.allow_protected_push_branch", True)
    assert pushq._refusal(p, "origin", "main", True) == "" and pushq._refusal(p, "origin", "HEAD", True)
    # The checks still run on the batch.
    bad = [_entry(repo, "e2", {"broken.txt": "x\n"})]
    rc, m = _run_batch(p, _batch_marker(p, bad, bid="b2", target="origin/main"), monkeypatch)
    assert _statuses(m) != ["pushed"] and _git_out(origin, "rev-parse", "main") == before, m
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b3", target="origin/main"), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed"], m
    assert _git_out(origin, "show", "main:f1.txt") == "1" and _git_out(origin, "rev-parse", "main~1") == before


def test_push_checks_accept_the_forms_config_set_sends(env):
    sys.path.insert(0, str(RUNTIME))
    try:
        from ttp.push import check_list
    finally:
        sys.path.remove(str(RUNTIME))
    assert check_list('["pytest -q", "make lint"]') == ["pytest -q", "make lint"]
    assert check_list("pytest -q\n\nmake lint\n") == ["pytest -q", "make lint"]
    assert check_list(["pytest -q", " "]) == ["pytest -q"]
    assert check_list(None) == []


def test_a_push_that_waits_past_push_wait_s_exits_75_with_a_probe(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_wait_s", 1)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    from ttp import locks, push
    held = locks.try_take(push.lock_paths(p, "origin", "proj"), "task #7 (run 9)")
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    t0 = time.time()
    assert _ttp_push() == 75
    assert 1 <= time.time() - t0 < 15
    err = capsys.readouterr().err
    assert "task #7 (run 9)" in err and "retry_when: " in err, err
    assert _git_out(origin, "rev-parse", "proj") == before
    (w,) = json.loads((run_dir / "lock_waits.json").read_text()).values()
    assert w["end"] - w["start"] >= 1 and locks.waited(run_dir) >= 1, "the wait must stop the run's clock"
    # The printed probe works where the harness runs it: in the project root, without the run's env.
    probe = err.split("retry_when: ")[1].strip()
    assert probe.endswith("ttp push --free")
    penv = {k: v for k, v in os.environ.items() if not k.startswith("TTP_") or k == "TTP_HOME"}
    run_probe = lambda: subprocess.run(probe, shell=True, cwd=str(p.root), env=penv, capture_output=True).returncode
    assert run_probe() == 1
    held.close()
    assert run_probe() == 0


def test_push_free_tells_whether_a_push_holds_the_branch(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    from ttp import locks, push
    paths = push.lock_paths(p, "origin", "proj")
    assert _ttp("push", "--free") == 0 and not any(x.exists() for x in paths), "--free must not create the lock"
    held = locks.try_take(paths, "someone")
    assert _ttp("push", "--free") == 1
    # The harness runs the probe in the project root, which need not have the pushing repo's remotes.
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _ttp("push", "--free") == 1
    held.close()
    assert _ttp("push", "--free") == 0


def test_a_detached_push_returns_at_once_and_its_marker_reports_the_pushed_sha_and_version(env, monkeypatch, capsys):
    started = env["tmp"] / "started"
    gate = env["tmp"] / "gate"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [_gated_check(started, gate)])
    pids = []
    try:
        _commit(repo, "plugins/p/rt/x.py", "x = 1\n")
        t0 = time.time()
        rc, marker, probe = _detach(capsys)
        pids += [json.loads(marker.read_text())["pid"]] if marker else []
        assert rc == 0 and time.time() - t0 < 10, "--detach must return at once"
        assert marker.parent == p.state / "pushes" and probe.endswith(f"ttp push --result {marker}")
        m = json.loads(marker.read_text())
        assert m["status"] == "running" and m["pid"] > 0 and m["target"] == "origin/proj" and m["log"]
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        r = _probe(p, probe)
        assert r.returncode == 1 and "still running" in r.stdout, r
        assert _ttp("push", "--free") == 1, "the detached push holds the branch's turn"
        from ttp import release
        assert release.push_in_flight(p), "an upgrade must not swap the runtime under a detached push"
        gate.touch()
        r = _probe_until_done(p, probe)
        pushed = _git_out(origin, "rev-parse", "proj")
        assert r.returncode == 0 and f"pushed {pushed}" in r.stdout and "version 0.1.1" in r.stdout, r
        m = json.loads(marker.read_text())
        assert (m["status"], m["exit"], m["sha"], m["version"]) == ("pushed", 0, pushed, "0.1.1")
        assert m["ended"] >= m["started"] and "bumped" in pathlib.Path(m["log"]).read_text()
        _wait_gone(m["pid"], time.time() + 10)   # the outcome is written a moment before the lock goes
        assert _ttp("push", "--free") == 0 and not release.push_in_flight(p)
        from ttp import push
        assert not push._run_lock(marker).exists(), "a finished push leaves no lock file behind"
        # A month later the next detached push tidies the finished marker and its log away.
        month = time.time() - push.KEEP_S - 60
        os.utime(marker, (month, month))
        _commit(repo, "plugins/p/rt/y.py", "y = 1\n")
        gate.unlink()
        rc, second, probe2 = _detach(capsys)
        pids += [json.loads(second.read_text())["pid"]] if second else []
        assert rc == 0 and not marker.exists() and not pathlib.Path(m["log"]).exists() and second.exists()
        gate.touch()
        assert _probe_until_done(p, probe2).returncode == 0
    finally:
        _release_detached(gate, pids)


def test_push_picks_a_branchs_real_base_over_the_push_target_and_other_task_branches(env, monkeypatch):
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    real = lambda: push.real_base(p, repo, "origin", "ttp/t48-int",
                                  _git_out(repo, "symbolic-ref", "--short", "HEAD"))
    # Based on main, with ttp/t47-near (also main-based) and t49 present: main, never a ttp/* branch.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-h3", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    assert real() == ("origin/main", 1)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t60-h3") == ("origin/main", 1)
    # Its upstream (a colleague's branch) when it tracks one.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t61-x", "--track", "origin/someone/feature")
    _commit(repo, "x.txt", "x\n")
    assert real() == ("origin/someone/feature", 1)
    # Based on the push target or on base_ref: the configured flow, as before.
    _git_out(repo, "checkout", "-q", "-b", "ttp/t62-y", "origin/ttp/t48-int")
    _commit(repo, "y.txt", "y\n")
    assert real() == ("origin/ttp/t48-int", 1)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t62-y") is None
    _git_out(repo, "checkout", "-q", "-b", "ttp/t63-z", "origin/rt")
    _commit(repo, "z.txt", "z\n")
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t63-z") is None


def test_push_keeps_a_branch_on_the_push_target_that_merged_in_another_branch(env, monkeypatch):
    """A branch that descends from the push target's tip but merged in main (not base_ref), with main
    the candidate it has the fewest commits over, is on the flow: never refused as foreign."""
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    _git_out(repo, "checkout", "-q", "--detach", "origin/main")
    _commit(repo, "m3.txt", "m3\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git_out(repo, "fetch", "-q", "origin")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t64-merged", "origin/ttp/t48-int")
    _git_out(repo, "merge", "-q", "--no-edit", "origin/main")
    _commit(repo, "w.txt", "w\n")
    assert push.real_base(p, repo, "origin", "ttp/t48-int", "ttp/t64-merged")[0] == "origin/main"
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t64-merged") is None


def test_a_detached_push_starts_promptly_when_the_base_detection_fetch_times_out(env, monkeypatch, capsys):
    """foreign_base runs in `ttp push --detach`'s foreground: its fetch of the push target is bounded,
    prompt-free and stdin-free, and a timeout skips base detection (as a failed fetch does), so the
    detached push still starts at once."""
    from ttp import push
    p, repo, origin = _foreign_setup(env, monkeypatch)
    target = _git_out(origin, "rev-parse", "ttp/t48-int")
    monkeypatch.setenv("TTP_TASK", "60")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-h3", "origin/main")
    _commit(repo, "mine.txt", "mine\n")
    real, bounded, unbounded = subprocess.run, [], []

    def stalled(cmd, *a, **kw):
        if "fetch" in cmd and cmd[-1].endswith(":refs/remotes/origin/ttp/t48-int"):
            if kw.get("timeout") is None:
                unbounded.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "would hang")
            bounded.append(kw)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return real(cmd, *a, **kw)
    monkeypatch.setattr(push.subprocess, "run", stalled)
    assert push.foreign_base(p, repo, "origin", "ttp/t48-int", "ttp/t60-h3") is None
    capsys.readouterr()
    start = time.monotonic()
    rc, marker, probe = _detach(capsys)
    assert rc == 0 and marker is not None and time.monotonic() - start < 30
    assert not unbounded, unbounded
    kw = bounded[0]
    assert kw["timeout"] == push.REACH_FETCH_S <= 60 and kw["stdin"] == subprocess.DEVNULL, kw
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0", kw
    monkeypatch.setattr(push.subprocess, "run", real)
    assert _probe_until_done(p, probe).returncode != 1
    assert _git_out(origin, "rev-parse", "ttp/t48-int") == target


def test_push_refuses_a_worktree_on_another_tasks_branch_at_once(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "checkout", "-q", "-b", "ttp/t149-fix")
    _commit(repo, "mine.txt", "mine\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t149-fix")   # published with --own
    shared = _git_out(origin, "rev-parse", "proj")
    worker = p.db.add_task("device check", kind="work")
    monkeypatch.setenv("TTP_TASK", str(worker))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        err = capsys.readouterr().err
        assert "task #149's branch ttp/t149-fix" in err and f"ttp/t{worker}-" in err, err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # The review of that change, a task built on it, or one that carries it as its branch may.
    review = p.db.add_task("review", kind="review")
    after = p.db.add_task("after", kind="code", depends_on=[149])
    carrier = p.db.add_task("carrier", kind="code", branch="ttp/t149-fix")
    for tid in (review, after, carrier):
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert push.resolve(p, repo) == ("origin", "proj", False), tid
    monkeypatch.delenv("TTP_TASK")          # outside a run: the configured target as before
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_push_from_a_tasks_own_branch_lands_only_when_its_spec_asks_or_land_says_so(env, monkeypatch, capsys):
    # Replays the upstream note: from a task worktree on ttp/t<id>-..., whose spec allows pushing only that
    # branch, `ttp push --detach` targeted the push branch. It is refused at once, naming --own and --land.
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    own_only = p.db.add_task("fix", kind="code", spec="Fix it. Push only your own ttp/* branch; never open PRs.")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{own_only}-fix")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", str(own_only))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        captured = capsys.readouterr()
        assert "pushing" not in captured.out and "origin/proj" not in captured.out, captured.out
        assert "does not ask it to land on origin/proj" in captured.err and "ttp push --own --detach" in captured.err \
            and "ttp push --land --detach" in captured.err, captured.err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # --own publishes the own branch, never the push branch.
    assert push.resolve(p, repo, True) == ("origin", f"ttp/t{own_only}-fix", True)
    # --land is the explicit target: it lands, also in the detached process (QUEUE_ENV).
    assert push.LAND_ENV in push.QUEUE_ENV
    monkeypatch.setenv(push.LAND_ENV, "1")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv(push.LAND_ENV)
    assert _ttp("push", "--land", "--own") != 0
    _record_checks_pass(p, own_only, repo)   # a code task lands only tested work (untested_landing)
    # A spec with a landing step lands as before; a `no_push:` line or a negated step never counts.
    for spec, lands in (("Then land it with `ttp push`.", True), ("Land it on the push branch.", True),
                        ("Run `ttp push`.\nno_push: experiment only", False), ("Do not run `ttp push`.", False)):
        p.db.conn.execute("UPDATE tasks SET spec=? WHERE id=?", (spec, own_only))
        p.db.conn.commit()
        try:
            got = push.resolve(p, repo)
        except ValueError as e:
            got = str(e)
        assert (got == ("origin", "proj", False)) is lands, (spec, got)
    # A pr_branch label naming the push branch is an explicit target; with own pushes off the push
    # branch is the only one. Both land without a landing step in the spec.
    p.db.conn.execute("UPDATE tasks SET spec=? WHERE id=?", ("Fix it.", own_only))
    p.db.conn.commit()
    with pytest.raises(ValueError, match="does not ask it to land"):
        push.resolve(p, repo)
    p.db.conn.execute("UPDATE tasks SET labels=? WHERE id=?", ('["pr_branch:proj"]', own_only))
    p.db.conn.commit()
    assert push.resolve(p, repo) == ("origin", "proj", False)
    p.db.conn.execute("UPDATE tasks SET labels=? WHERE id=?", ("[]", own_only))
    p.db.conn.commit()
    p.set_config("delivery.own_push", "off")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    p.set_config("delivery.own_push", "on")
    # A review lands from its own branch as before; outside a run nothing is gated.
    review = p.db.add_task("review", kind="review")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{review}-review")
    monkeypatch.setenv("TTP_TASK", str(review))
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def _record_checks_pass(p, tid, repo, head=None, passed=True):
    """A `ttp checks` record in a run of task `tid` on `head` (default HEAD) of worktree `repo`."""
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running"))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checks.json").write_text(json.dumps({
        "head": head or _git_out(repo, "rev-parse", "HEAD"), "worktree": str(repo), "passed": passed,
        "commands": ["true"], "skipped": [], "ts": time.time()}))
    return run_dir


def test_a_code_workers_plain_push_lands_only_tested_work_unless_land_overrides_it(env, monkeypatch, capsys, tmp_path):
    # Replays the upstream note: a code task's worker ran plain `ttp push --detach` and it rebased the
    # task branch onto the push branch and pushed untested work there. A code task whose spec says to
    # land now needs `ttp checks` passed on HEAD; `--land` overrides it, explicitly and logged.
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    tid = p.db.add_task("fix", kind="code", spec="Fix it, then land it with `ttp push`.")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{tid}-fix")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", str(tid))
    for args in (["push"], ["push", "--detach"]):
        assert _ttp(*args) == 2, args
        err = capsys.readouterr().err
        assert "`ttp checks` has not passed on this commit" in err and "ttp push --own --detach" in err \
            and "ttp push --land --detach" in err, err
    assert not (p.state / "pushes").exists() and _git_out(origin, "rev-parse", "proj") == shared
    # A failed record, a pass on another commit or a dirty tree is not tested work.
    _record_checks_pass(p, tid, repo, passed=False)
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _record_checks_pass(p, tid, repo, head=_git_out(repo, "rev-parse", "HEAD~1"))
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _record_checks_pass(p, tid, repo)
    assert push.resolve(p, repo) == ("origin", "proj", False)
    (repo / "mine.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    _git_out(repo, "checkout", "-q", "--", "mine.txt")
    # A new commit after the pass needs its own.
    _commit(repo, "more.txt", "more\n")
    with pytest.raises(ValueError, match="has not passed"):
        push.resolve(p, repo)
    # --land is the explicit override: it lands untested, and says so on stderr and in the run's notes.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    monkeypatch.setenv(push.LAND_ENV, "1")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    assert "--land: task #%d lands its own branch" % tid in capsys.readouterr().err
    assert "explicit override of the landing guard" in (run_dir / "progress.md").read_text()
    monkeypatch.delenv(push.LAND_ENV)
    # With own pushes off the refusal does not offer --own.
    p.set_config("delivery.own_push", "off")
    with pytest.raises(ValueError) as e:
        push.resolve(p, repo)
    assert "--own" not in str(e.value) and "ttp checks" in str(e.value)
    p.set_config("delivery.own_push", "on")
    # Unchanged: a review and other task kinds land untested as before (the push runs the checks
    # itself), and the push queue (no task in its environment) is not gated.
    for kind in ("review", "work"):
        other_tid = p.db.add_task(kind, "Land it with `ttp push`.", kind=kind)
        _git_out(repo, "checkout", "-q", "-B", f"ttp/t{other_tid}-x")
        monkeypatch.setenv("TTP_TASK", str(other_tid))
        assert push.resolve(p, repo) == ("origin", "proj", False), kind
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_push_defaults_to_the_tasks_own_branch_on_the_remote_without_a_push_branch(env, monkeypatch, capsys):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    shared = _git_out(origin, "rev-parse", "proj")
    p.set_config("delivery.push_branch", "")
    _git_out(repo, "checkout", "-q", "-b", "ttp/t60-exp")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", "60")
    # Not on the remote yet: nothing to default to, the usual refusal.
    assert _ttp("push") == 2 and "set delivery.push_branch" in capsys.readouterr().err
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/ttp/t60-exp")
    _commit(repo, "more.txt", "more\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    assert push.resolve(p, repo) == ("origin", "ttp/t60-exp", True)
    rc = _ttp("push", "--detach")
    out = capsys.readouterr().out
    probe = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("retry_when: "))
    assert rc == 0 and "origin/ttp/t60-exp" in out
    r = _probe_until_done(p, probe)
    assert r.returncode == 0 and f"pushed {head} to origin/ttp/t60-exp" in r.stdout, r
    assert _git_out(origin, "rev-parse", "ttp/t60-exp") == head and _git_out(origin, "rev-parse", "proj") == shared
    # A configured push branch still wins over the own branch.
    p.set_config("delivery.push_branch", "origin/proj")
    assert push.resolve(p, repo) == ("origin", "proj", False)
    assert _ttp("push") == 0 and _git_out(origin, "rev-parse", "proj") == head

def test_a_dead_detached_push_reads_as_failed_and_frees_the_branch(env, monkeypatch, capsys):
    started = env["tmp"] / "started"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {started}; sleep 60"])
    _commit(repo, "mine.txt", "mine\n")
    rc, marker, probe = _detach(capsys)
    pid = json.loads(marker.read_text())["pid"]
    try:
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert started.exists() and _probe(p, probe).returncode == 1
        assert _ttp("push", "--free") == 1
        os.kill(pid, signal.SIGKILL)     # a crash or a reboot: no outcome is written
        deadline = time.time() + 10
        while _ttp("push", "--free") != 0 and time.time() < deadline:
            time.sleep(0.05)
        assert _ttp("push", "--free") == 0, "a dead push must free the branch"
        r = _probe(p, probe)
        assert r.returncode == 0 and "not pushed" in r.stdout and "without writing an outcome" in r.stdout, r
        m = json.loads(marker.read_text())
        assert m["status"] == "failed" and m["exit"] is None and m["reason"]
        from ttp import push
        assert not push._run_lock(marker).exists()
        assert _probe(p, probe).returncode == 0
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_free(marker, timeout=10):
    from ttp import push, locks
    deadline = time.time() + timeout
    while not locks.any_free([push._run_lock(marker)]) and time.time() < deadline:
        time.sleep(0.05)


def test_a_detached_push_killed_with_its_launcher_leaves_a_log_and_says_it_never_began(env, monkeypatch, capsys):
    """A sandbox that ends the command's processes kills the push before it logs a line. The log
    must still say what happened and the verdict must name the early death, also for the daemon."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "mine.txt", "mine\n")
    rc, marker, probe = _detach(capsys)
    pid = json.loads(marker.read_text())["pid"]
    os.kill(pid, signal.SIGKILL)         # the sandbox ends with the command: no outcome, no output
    _wait_free(marker)
    from ttp import push
    assert pathlib.Path(json.loads(marker.read_text())["log"]).read_text().strip(), "the log is never empty"
    push.tend(p)                         # the daemon records the verdict without waiting for the probe
    m = json.loads(marker.read_text())
    assert m["status"] == "failed" and m["exit"] is None and "before it began" in m["reason"], m
    r = _probe(p, probe)
    assert r.returncode == 0 and "without writing an outcome before it began" in r.stdout, r
    assert "starting the push process" in r.stdout, "the log tail comes with the verdict"


def test_a_detached_push_from_a_sandboxed_command_is_queued_and_the_daemon_starts_it(env, monkeypatch, capsys):
    """Inside a PID namespace other than the run's, nothing started survives the command: the push
    is queued and the daemon starts it, with the run's task for `--own`."""
    from ttp import push
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "checkout", "-q", "-b", "ttp/t7-exp")
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")   # the run's namespace, not this one
    rc = _ttp("push", "--own", "--detach")
    out = capsys.readouterr().out
    marker = pathlib.Path(next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("marker: ")))
    probe = next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("retry_when: "))
    m = json.loads(marker.read_text())
    assert rc == 0 and "queued" in out and m["status"] == "queued" and "pid" not in m, out
    assert m["env"]["TTP_TASK"] == "7" and "TTP_PIDNS" not in m["env"]
    r = _probe(p, probe)
    assert r.returncode == 1 and "queued for the daemon" in r.stdout, r
    assert [x["id"] for x in push.running(p)] == [m["id"]], "its worktree stays while it is queued"
    monkeypatch.delenv("TTP_TASK")       # the daemon has none: the marker carries it
    monkeypatch.delenv("TTP_PIDNS")
    push.tend(p)
    assert json.loads(marker.read_text())["status"] in ("running", "pushed")
    r = _probe_until_done(p, probe)
    head = _git_out(repo, "rev-parse", "HEAD")
    assert r.returncode == 0 and f"pushed {head} to origin/ttp/t7-exp" in r.stdout, r
    assert _git_out(origin, "rev-parse", "ttp/t7-exp") == head


def test_a_queued_push_the_daemon_never_starts_reads_as_failed(env, monkeypatch, capsys):
    from ttp import push
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "mine.txt", "mine\n")
    monkeypatch.setenv("TTP_PIDNS", "pid:[1]")
    rc, marker, probe = _detach(capsys)
    m = json.loads(marker.read_text())
    m["queued"] -= push.QUEUED_S + 60
    marker.write_text(json.dumps(m))
    r = _probe(p, probe)
    assert r.returncode == 0 and "did not start the queued push" in r.stdout, r
    push.tend(p)
    assert json.loads(marker.read_text())["status"] == "failed", "a failed push is never started late"
    assert _git_out(origin, "rev-parse", "proj") != _git_out(repo, "rev-parse", "HEAD")


def test_a_push_that_died_in_a_reboot_says_so(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = p.state / push.DETACHED / "t1-x.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"id": "t1-x", "status": "running", "pid": 1, "boot": "an-earlier-boot",
                                  "alive": time.time(), "log": str(marker.with_suffix(".log"))}))
    push.tend(p)
    assert "the host rebooted while it ran" in json.loads(marker.read_text())["reason"]


def test_a_malformed_queued_push_marker_is_recorded_as_failed_and_the_tick_goes_on(env):
    """A queued marker the daemon cannot use (a non-numeric queued time, no repo or log, an env that
    is not a dict) is recorded as a failed push with the reason, and the rest of the tick still runs."""
    from ttp import push
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    now = time.time()
    bad = {"a-queued": {"queued": "soon"},
           "b-repo": {"queued": now, "log": str(folder / "b-repo.log")},
           "c-log": {"queued": now, "repo": str(p.root)},
           "d-env": {"queued": now, "repo": str(p.root), "log": str(folder / "d-env.log"), "env": ["X=1"]}}
    for rid, m in bad.items():
        from ttp import checkdirs
        (folder / f"{rid}.json").write_text(json.dumps({"id": rid, "status": "queued",
                                                     "check_run_dir": str(checkdirs.new(p.state)), **m}))
    tid = p.db.add_task("after the pushes", "s", kind="work", tier="light", origin="user")
    d.tick()
    assert p.db.q("SELECT id FROM runs WHERE task=?", (tid,)), "a malformed push marker stopped the tick"
    for rid in bad:
        m = json.loads((folder / f"{rid}.json").read_text())
        assert m["status"] == "failed" and m["exit"] is None and "malformed" in m["reason"], m
    d.tick()                             # recorded once: never retried or raised again
    assert all(json.loads((folder / f"{rid}.json").read_text())["status"] == "failed" for rid in bad)


def test_the_push_result_probe_reports_a_malformed_queued_marker_as_failed(env, capsys):
    """`ttp push --result` on a queued marker with a non-numeric queued time gives a verdict, not a traceback."""
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "queued", "queued": "soon"}))
    assert push.result(marker) == 0
    assert "not pushed" in capsys.readouterr().out and "malformed" in json.loads(marker.read_text())["reason"]


def test_the_push_result_probe_reports_a_malformed_marker_it_cannot_rewrite(env, capsys, monkeypatch):
    """When the failed verdict cannot be written back, the probe still reports it, not a traceback."""
    from ttp import push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "queued", "queued": "soon"}))
    def unwritable(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(push, "write_json", unwritable)
    assert push.result(marker) == 0
    out = capsys.readouterr().out
    assert "not pushed (malformed marker" in out, out


def test_the_push_result_probe_reports_a_running_marker_with_a_bad_start_as_failed(env, capsys):
    """A running marker whose start time is not a number gets a verdict, not a traceback."""
    from ttp import locks, push
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True)
    marker = folder / "x.json"
    marker.write_text(json.dumps({"id": "x", "status": "running", "started": "soon", "pid": 1}))
    held = locks.try_take([push._run_lock(marker)], "detached push x")   # it really runs
    try:
        assert push.result(marker) == 0
    finally:
        held.close()
    out = capsys.readouterr().out
    assert "not pushed (malformed marker" in out, out

def test_runs_carry_the_daemons_pid_namespace(env):
    from ttp import push
    from ttp.daemon import Daemon
    if not push.pid_ns():
        pytest.skip("no /proc/self/ns/pid")
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    cwd, _ = d._workdir_for(p.db.task(tid))
    rid = d.start_run("worker", "go", "claude", "light", cwd, task=p.db.task(tid))
    (p.runs / str(rid) / "STOP").touch()
    assert json.loads((p.runs / str(rid) / "run.json").read_text())["env"]["TTP_PIDNS"] == push.pid_ns()


def test_version_bump_config_is_validated(env):
    from ttp import push
    from ttp.project import config_problems
    assert push.bump_of(None) is None and push.bump_of({}) is None
    assert push.bump_of('{"files": ["a/x.json", "a/b/y.py"]}') == \
        {"files": ["a/x.json", "a/b/y.py"], "paths": ["a"], "package": "", "changeset_dir": ""}
    for bad in ("x.json", {"files": []}, {"files": ["/abs.json"]}, {"files": ["../up.json"]},
                {"files": ["x.json"], "extra": 1}, [1]):
        with pytest.raises(ValueError):
            push.bump_of(bad)
    assert config_problems({"delivery": {"version_bump": {"files": []}}}) == \
        ["delivery.version_bump.files must list the files that hold the version"]
    assert config_problems({"delivery": {"version_bump": {"files": ["a/x.json"]}}}) == []


def test_push_excluded_helper_ignores_deletions_and_patches_already_on_the_tip(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tip = _git_out(repo, "rev-parse", "HEAD")
    assert push.exclude_list(None) == [] and push.exclude_list("") == []
    assert push.exclude_list('["./tmp/", "notes/*.md"]') == ["tmp", "notes/*.md"]
    (repo / "tmp").mkdir()
    _commit(repo, "tmp/a.txt", "a\n")
    _commit(repo, "x.py", "1\n")
    assert push.excluded(repo, tip, "HEAD", ["tmp"]) == ["tmp/a.txt"]
    assert push.excluded(repo, tip, "HEAD", []) == []
    assert push.excluded(repo, "HEAD~1", "HEAD", ["tmp"]) == []
    _git_out(repo, "rm", "-q", "tmp/a.txt")
    _git_out(repo, "commit", "-qm", "rm")
    assert push.excluded(repo, "HEAD~1", "HEAD", ["tmp/**"]) == []


def test_a_push_batch_fast_forwards_the_extra_branches_it_is_given(env, monkeypatch, capsys):
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git_out(other, "fetch", "-q", "origin")
    _git_out(other, "checkout", "-q", "-b", "old", "origin/main")
    _commit(other, "old.txt", "old\n")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/old")
    old = _git_out(origin, "rev-parse", "old")
    p.set_config("delivery.fast_forward_also", ["main", "old"])
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    marker = _batch_marker(p, heads)
    rc, m = _run_batch(p, marker, monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and m["pushed_sha"] == sha, m
    assert _git_out(origin, "rev-parse", "main") == sha and _git_out(origin, "rev-parse", "old") == old
    assert m["fast_forward"][0] == f"ff main {sha}" and m["fast_forward"][1].startswith("not ff old: "), m
    capsys.readouterr()
    assert batch.summary(marker) == 0
    out = capsys.readouterr().out
    assert f"  ff main {sha}" in out and "  warning: not ff old: " in out



def test_a_push_batch_mirrors_its_pushed_commit_and_still_runs_after_push(env, monkeypatch, capsys):
    from ttp import batch
    p, repo, origin, mirror = _mirror_setup(env, monkeypatch)
    deployed = env["tmp"] / "deployed"
    p.set_config("delivery.after_push", [f'echo "$TTP_PUSHED_SHA" > {deployed}'])
    heads = [_entry(repo, "e1", {"f1.txt": "1\n"})]
    marker = _batch_marker(p, heads)
    rc, m = _run_batch(p, marker, monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and m["pushed_sha"] == sha, m
    assert _git_out(mirror, "rev-parse", "dev") == sha and m["fast_forward"] == [f"mirror mirror/dev {sha}"], m
    assert m["after_push"]["status"] == "ok" and deployed.read_text().strip() == sha, "after_push still runs"
    capsys.readouterr()
    assert batch.summary(marker) == 0 and f"  mirror mirror/dev {sha}" in capsys.readouterr().out
    # A diverged mirror: the batch still pushes, the mirror stays, after_push still runs.
    side = env["tmp"] / "side"
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(mirror), str(side)], check=True)
    _commit(side, "hotfix.txt", "fix\n")
    _git_out(side, "push", "-q", "origin", "HEAD:dev")
    theirs = _git_out(mirror, "rev-parse", "dev")
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"f2.txt": "2\n"})], bid="b2"), monkeypatch)
    sha = _git_out(origin, "rev-parse", "proj")
    assert rc == 0 and m["outcome"] == "pushed" and _git_out(mirror, "rev-parse", "dev") == theirs, m
    [line] = m["fast_forward"]
    assert line.startswith("not mirrored mirror/dev: ") and theirs in line and sha in line, line
    assert deployed.read_text().strip() == sha


def test_a_push_batch_names_each_entrys_own_rebased_commit_and_the_queue_records_it(env, monkeypatch):
    """A rebase lands each entry under a new sha: the marker names it per entry ("sha"), and the push
    queue keeps it per row (landed_sha), so `landed:#id` can find the task's change on the branch."""
    from ttp import pushq
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in (1, 2)]
    on = _entry(repo, "e0", {"plugins/p/f0.txt": "0\n"})
    _git_out(repo, "push", "-q", "origin", f"{on}:refs/heads/proj")
    rc, m = _run_batch(p, _batch_marker(p, heads + [on]), monkeypatch)
    assert rc == 0 and _statuses(m) == ["pushed", "pushed", "landed"], m
    shas = [r.get("sha") for r in m["results"]]
    tip = _git_out(origin, "rev-parse", "proj")
    assert shas[2] == on and shas[0] != heads[0] and shas[1] != heads[1] and m["pushed_sha"] == tip
    assert _git_out(origin, "log", "-1", "--format=%s", shas[1]) == "e2: edit plugins/p/f2.txt"
    assert _git_out(origin, "rev-parse", f"{shas[1]}^") == shas[0] and _git_out(origin, "rev-parse", f"{tip}^") == shas[1]
    # pushq._apply keeps each row's own commit, and the batch's head for a row the marker names none of.
    now = time.time()
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1', 'm', 'origin/proj', ?)", (now,))
    for n, h in enumerate(heads + [on], 1):
        p.db.x("INSERT INTO push_queue(id, task, branch, head, target, status, batch, created, updated) "
               "VALUES(?, ?, ?, ?, 'origin/proj', 'batched', 'b1', ?, ?)", (n, 100 + n, f"e{n}", h, now, now))
    m["results"][1].pop("sha")
    pushq._apply(p, {"id": "b1", "target": "origin/proj"}, m, lambda *a, **k: None, now)
    assert [r["landed_sha"] for r in p.db.q("SELECT landed_sha FROM push_queue ORDER BY id")] == [shas[0], tip, on]


def test_merge3_keeps_lines_both_sides_added_and_leaves_real_conflicts(env):
    from ttp.batch import merge3
    assert merge3("a\nX\n", "a\n", "a\nY\n") == "a\nX\nY\n"
    assert merge3("a\nX\nb\n", "a\nb\n", "a\nY\nb\n") == "a\nX\nY\nb\n"
    assert merge3("X\na\nb\n", "a\nb\n", "a\nb\nY\n") == "X\na\nb\nY\n", "a clean merge is kept as it is"
    assert merge3("a\nX\n", "a\nb\n", "a\nY\n") is None, "both changed one line"
    assert merge3("a\nX\n", "a", "a\nY\n") is None, "a last line without a newline is a change of it"
    assert merge3("a\nX\n", "a\n", "a\n=======\nY\n") is None, "a line like a separator is ambiguous"
    assert merge3("a\n=======\nX\n", "a\n", "a\nY\n") == "a\n=======\nX\nY\n"
    old, ours = "def x():\n    pass\n", "def x():\n    pass\n\n\ndef a():\n    return 1\n"
    for gap in ("", "\n\n\n"):
        theirs = old + gap + "def b():\n    return 2\n"
        assert merge3(ours, old, theirs, py=True) == ours + "\n\ndef b():\n    return 2\n", "PEP 8's two blank lines"
        assert merge3(ours, old, theirs) == ours + gap + "def b():\n    return 2\n", "elsewhere each side's spacing"
    assert merge3(ours, old, old + "Y = 2\n", py=True) == ours + "Y = 2\n"


def test_merge3_taken_in_takes_theirs_only_where_it_holds_our_change(env):
    """`taken_in` (template upgrades): a conflict whose local change upstream already ships takes
    upstream's side; any other conflict is still left for judgment, and the default is unchanged."""
    from ttp.batch import merge3
    base, ours = "a\nline1\nline2\nz\n", "a\nline1 fixed\nline2\nz\n"
    theirs = "a\nline1 fixed\nline2 improved\nz\n"
    assert merge3(ours, base, theirs) is None
    assert merge3(ours, base, theirs, taken_in=True) == theirs
    assert merge3("a\nX fix\nz\n", "a\nz\n", "a\nW\nX fix\nz\n", taken_in=True) == "a\nW\nX fix\nz\n"
    assert merge3("a\nX\nz\n", "a\nz\n", "a\nY\nz\n", taken_in=True) == "a\nX\nY\nz\n", "additions keep both"
    assert merge3("a\nmine\nz\n", "a\nold\nz\n", "a\nnew\nz\n", taken_in=True) is None, "both changed it"
    assert merge3("a\nz\n", "a\nold\nz\n", "a\nold new\nz\n", taken_in=True) is None, \
        "ours removed a line theirs changed"
    assert merge3("a\nfix\nz\n", "a\nold\nz\n", "a\nfix\nold\nz\n", taken_in=True) is None, \
        "theirs keeps a line ours removed"


def test_merge3_never_keeps_a_line_twice_or_moves_one_into_another_block(env):
    """Keeping both additions is right only when they share no line: two changes adding one import,
    or a change replayed onto a tip that holds it already, would leave that line twice. Lines of
    brackets, punctuation or quotes alone repeat nothing, nor does a line in the body of a new
    top-level Python def (its name is checked apart)."""
    from ttp.batch import merge3
    assert merge3("x\nA1\nB1\ny\n", "x\ny\n", "x\nB1\nB2\ny\n") is None, "a shared line in larger additions"
    assert merge3("x\nA1\nB1\ny\n", "x\ny\n", "x\nB1\ny\n") is None, "theirs is in ours already"
    assert merge3("x\nB1\ny\n", "x\ny\n", "x\nA1\nB1\ny\n") is None, "ours is in theirs"
    assert merge3("x\nA1\n  - B1\ny\n", "x\ny\n", "x\n- B1\ny\n") is None, "indented alike or not"
    assert merge3("x\nf(1,\n)\n'''\ny\n", "x\ny\n", "x\ng(2,\n)\n'''\ny\n") == "x\nf(1,\n)\n'''\ng(2,\n)\n'''\ny\n"
    old = "import os\n\n\ndef x():\n    pass\n"
    ours = old + "\n\n@pytest.mark.slow\ndef test_a(env):\n    p = make(env)\n    assert p\n"
    theirs = old + "\n\n@pytest.mark.slow\ndef test_b(env):\n    p = make(env)\n    assert not p\n"
    assert merge3(ours, old, theirs, py=True) == ours + theirs[len(old):], "two new tests"
    assert merge3(ours, old, theirs) is None, "outside Python, a line is a line"
    assert merge3(ours, old, old + "\n\ndef test_a(env):\n    return\n", py=True) is None, "a def both add"
    assert merge3("import os\nimport re\n", "import os\n", "import os\nimport re\nimport sys\n", py=True) is None
    assert merge3("@slow\n@a\ndef f():\n", "def f():\n", "@slow\n@b\ndef f():\n", py=True) is None, \
        "a decorator both put on one old def"
    # Theirs continues the block it was written in; after ours' new def it would land in that one.
    assert merge3(ours, old, old + "    assert x\n", py=True) is None
    assert merge3(old + "    assert x\n", old, ours, py=True) == old + "    assert x\n" + ours[len(old):]
    assert merge3("- a\n## New\n", "- a\n", "- a\n  - under a\n") is None
    assert merge3("- a\n  - b\n", "- a\n", "- a\n  - c\n") == "- a\n  - b\n  - c\n"


def test_merge3_hunks_takes_both_changes_only_where_they_touch_different_lines(env):
    """git calls changes of adjacent lines a conflict. With `hunks` (the push batch), a conflict whose
    two sides changed different lines of the base takes both; one where they changed the same line,
    or where one inserts at the edge of the other's change, or both add one line, stays a conflict."""
    from ttp.batch import merge3
    base = "a\nb\nc\nd\n"
    ours, theirs = "A\nb\nc\nd\n", "a\nB\nc\nd\n"
    assert merge3(ours, base, theirs) is None, "without hunks, git's conflict stands"
    kinds = set()
    assert merge3(ours, base, theirs, hunks=True, kinds=kinds) == "A\nB\nc\nd\n" and kinds == {"hunks"}
    assert merge3("a\nb\nc\n", "a\nb\nX\nc\n", "a\nB\nX\nc\n", hunks=True) == "a\nB\nc\n", "a deletion next to an edit"
    assert merge3("a\nX\nb\n", "a\nb\n", "a\nB\n", hunks=True) is None, "an insertion at the edge of an edit"
    assert merge3("a\nX\n", "a\nb\n", "a\nY\n", hunks=True) is None, "both changed one line"
    assert merge3("A\nimport os\nb\n", "a\nb\n", "a\nB\nimport os\n", hunks=True) is None, "both add one line"
    kinds = set()
    assert merge3("a\nX\n", "a\n", "a\nY\n", hunks=True, kinds=kinds) == "a\nX\nY\n" and kinds == {"added"}
    py = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    assert merge3(py.replace("if x:", "if x and y:"), py, py.replace("return 1", "return 2"), py=True, hunks=True) \
        == "def f(x):\n    if x and y:\n        return 2\n    return 0\n"


def test_merge3_keeps_the_final_newline_as_the_sides_had_it(env):
    """git ends each side of a conflict at the end of the file with a newline. The settled result ends
    as the sides had it: without one where none had it, with one where a side added it."""
    from ttp.batch import merge3
    assert merge3("a\nb", "a\nc", "z\nc", hunks=True) == "z\nb"
    assert merge3("a\r\nb", "a\r\nc", "z\r\nc", hunks=True) == "z\r\nb"
    assert merge3("a\nb\n", "a\nc\n", "z\nc\n", hunks=True) == "z\nb\n"
    assert merge3("a\nb", "a\nc", "z\nc\n", hunks=True) == "z\nb\n", "theirs added the newline"
    assert merge3("a\nb", "a\nc\n", "z\nc\n", hunks=True) == "z\nb", "ours took it away"
    assert merge3("A\nb\nc", "a\nb\nc", "a\nb\nC", hunks=True) == "A\nb\nC", "no conflict: git's own result"


def test_a_push_batch_counts_requeued_conflicts_and_not_rebase_stops(env, monkeypatch):
    """An entry that conflicts with one that then fails its checks is requeued, and still counts as
    conflicted; a rebase that stops without a conflict is sent back but counts as none."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -e bad.txt"])
    _commit(other, "notes.txt", "a\nb\nc\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    heads = [_entry(repo, "e1", {"more.txt": "1\n"}), _entry(repo, "e2", {"notes.txt": "a\nB2\nc\n", "bad.txt": "2\n"}),
             _entry(repo, "e3", {"notes.txt": "a\nB3\nc\n"})]
    rebase = batch.Batch._rebase

    def stop_e1(self, onto, top):
        return (onto, ["(the rebase stopped: hook said no)"]) if top == heads[0] else rebase(self, onto, top)
    monkeypatch.setattr(batch.Batch, "_rebase", stop_e1)
    rc, m = _run_batch(p, _batch_marker(p, heads), monkeypatch)
    assert _statuses(m) == ["conflict", "check_failed", "requeued"], m
    assert m["conflicts"] == {"entries": 3, "conflicted": 1, "auto_resolved": 0, "by_hunks": 0, "sent_back": 0}


def test_the_push_queue_counts_conflicts_and_those_its_batches_resolved(env):
    from ttp import pushq
    p, rid = _pq_rows(env)
    now = time.time()
    assert pushq.conflict_stats(p.db, now - 3600)["entries"] == 0
    for status, detail in (("pushed", None), ("pushed", {"settled": ["a.py"], "settled_by": ["hunks"]}),
                           ("check_failed", {"cmd": "pytest", "settled": ["b.py"], "settled_by": ["added"]}),
                           ("conflict", {"files": ["c.py"]}), ("approved", None),
                           ("conflict", {"files": ["d.py"], "settled": ["d.py"], "settled_by": ["hunks"]})):
        _pq_row(p, rid, now, status=status, batch="b1")
        p.db.x("UPDATE push_queue SET detail=? WHERE id=(SELECT MAX(id) FROM push_queue)",
               (json.dumps(detail) if detail else None,))
    _pq_row(p, rid, now, status="conflict", batch="b0")
    p.db.x("UPDATE push_queue SET updated=? WHERE id=(SELECT MAX(id) FROM push_queue)", (now - 8 * 86400,))
    want = {"entries": 5, "conflicted": 4, "auto_resolved": 2, "sent_back": 2, "conflict_pct": 80, "sent_back_pct": 40}
    assert pushq.summary(p, now)["conflicts"] == want, "a week's settled rows; the older one and approved ones are out"
    assert "last 7 days: 5 entries done, 4 conflicted (80%), 2 resolved in the batch, 2 sent back (40%)" \
        in pushq.queue_text(p, now).splitlines()
    # A rebase that stopped or did not finish is no conflict, as in Batch.conflicts().
    for files in (["(the rebase stopped: boom)"], ["(the rebase did not finish)"]):
        _pq_row(p, rid, now, status="conflict", batch="b2")
        p.db.x("UPDATE push_queue SET detail=? WHERE id=(SELECT MAX(id) FROM push_queue)",
               (json.dumps({"files": files}),))
    got = pushq.conflict_stats(p.db, now - 3600)
    assert (got["entries"], got["conflicted"], got["sent_back"]) == (7, 4, 2), got


def test_a_push_batch_rebuilds_a_worktree_a_crash_left_mid_rebase(env, monkeypatch, capsys):
    """The batch process dies while it settles a conflict: the next batch with those entries finds
    the push worktree mid-rebase, makes it anew and pushes them."""
    p, repo, origin, other = _bump_setup(env, monkeypatch, ["true"])
    _commit(other, "plugins/p/notes.txt", "a\nc\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    heads = [_entry(repo, f"e{n}", {"plugins/p/notes.txt": f"a\nX{n}\nc\n"}) for n in (1, 2)]
    before = _git_out(origin, "rev-parse", "proj")
    crash = ("import os, sys\nfrom ttp import batch, cli\n"
             "batch.settle = lambda *a, **k: os._exit(9)\ncli.main(['push', '--batch', sys.argv[1]])\n")
    marker = _batch_marker(p, heads)
    output = json.loads(marker.read_text())["check_report_dir"]
    r = subprocess.run([sys.executable, "-c", crash, str(marker)], cwd=str(repo), input="",
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RUN_DIR": output})
    from ttp import batch
    wt = p.worktrees / "push"
    assert r.returncode == 9 and batch._rebasing(wt), r.stdout + r.stderr
    assert _git_out(origin, "rev-parse", "proj") == before
    capsys.readouterr()
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b2"), monkeypatch)
    assert "preparing a fresh worktree" in capsys.readouterr().out
    assert rc == 0 and m["outcome"] == "pushed" and _statuses(m) == ["pushed", "pushed"], m
    assert _git_out(origin, "show", "proj:plugins/p/notes.txt") == "a\nX1\nX2\nc"
    assert _versions(origin) == ("0.1.1", "0.1.1") and not batch._rebasing(wt)


def test_the_daemon_leaves_a_push_batch_marker_to_the_batch(env, monkeypatch):
    """push.tend settles detached pushes by their run lock. A batch lets go of that lock before its
    after_push and writes its marker after it, so tend must not touch a batch marker: marking it
    failed would race the batch's last write, and its deploy would run again."""
    from ttp import locks, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})])
    from ttp.project import write_json
    ap_lock = p.state / "locks" / "after_push:run-b1.0.lock"
    write_json(marker, {**push._read(marker), "phase": "after_push", "outcome": "pushed", "lock": str(ap_lock)})
    before = marker.read_bytes()
    held = locks.try_take([ap_lock], "push batch b1 (after_push)")
    try:
        assert locks.any_free([push._run_lock(marker)]), "the run lock is free, as in the after_push"
        push.tend(p)
        assert marker.read_bytes() == before and push._settle(marker) == push._read(marker)
    finally:
        held.close()
    push.tend(p)          # the batch died: the push queue judges it (pushq.finalize), not tend
    assert marker.read_bytes() == before


@pytest.mark.parametrize("names, fresh, want, runs, tried", [
    (["fix", "e2", "bad"], 1, ["pushed", "pushed", "check_failed"], 4, None),   # fixing prefix passes; search above
    (["fix", "e2", "bad"], 3, ["pushed", "pushed", "check_failed"], 4, None),   # all untried: search the whole batch
    (["fix", "bad", "e3"], 2, ["pushed", "check_failed", "requeued"], 3, None),  # the breaker is in the untried prefix
    (["e1", "fix", "bad"], 3, ["requeued"] * 3, 2, [1]),    # prefix 1 fails: only entry 1 is shown not to fix it
    (["e1", "e2"], 2, ["requeued", "requeued"], 2, [1]),                       # all untried, none fixes it
    (["e1", "fix"], 2, ["pushed", "pushed"], 1, None),                         # tip with the batch passes: it goes
    (["e1", "e2"], 1, ["requeued", "requeued"], 2, [1]),                       # the fresh prefix does not fix it
    (["e1", "e2"], 0, ["requeued", "requeued"], 1, []),                        # nothing new: one check, no tip alone
])
def test_a_push_batch_on_a_known_failing_tip_never_checks_it_alone_and_pushes_a_fix(env, monkeypatch, capsys, names,
                                                                                     fresh, want, runs, tried):
    """A tip the queue knows fails on its own (marker "bad_tip") is not checked alone again. The tip with
    the batch, and the tip with the entries not yet tried on it, are; an entry that fixes the tip goes,
    with the ones after it up to the first that breaks the checks. A batch that ends at prefix 1 failing
    names only the entries it showed do not fix the tip ("tip_tried"): a fix after them stays untried."""
    from ttp.project import write_json
    from ttp import push
    log = env["tmp"] / "checks.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [
        f"echo run >> {log}", "test ! -e broken.txt || test -e plugins/p/fix.txt", "test ! -e plugins/p/bad.txt"])
    _commit(other, "broken.txt", "x\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    tip = _git_out(origin, "rev-parse", "proj")
    heads = [_entry(repo, n, {f"plugins/p/{n}.txt": f"{n}\n"}) for n in names]
    marker = _batch_marker(p, heads)
    write_json(marker, {**push._read(marker), "bad_tip": {"tip": tip, "fresh": fresh, "cmd": "make test"}})
    rc, m = _run_batch(p, marker, monkeypatch)
    assert _statuses(m) == want, m
    out = capsys.readouterr().out
    assert m["checks"]["runs"] == runs == len(log.read_text().split()) + out.count(": echo run"), m
    assert "checking the tip" not in out and "tip_passed" not in m
    if want[0] == "requeued":
        assert rc == 4 and m["outcome"] == "tip_failed" and _git_out(origin, "rev-parse", "proj") == tip
        assert m["tip_check"]["cmd"] == "make test" and "not checked alone" in m["message"]
        assert m["tip_tried"] == tried, m
        return
    assert rc == 0 and m["outcome"] == "pushed" and _git_out(origin, "show", "proj:plugins/p/fix.txt") == "fix"


def test_a_push_batch_finds_the_push_lock_busy_and_touches_nothing(env, monkeypatch):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = _batch_marker(p, [_entry(repo, "e1", {"f1.txt": "1\n"})])
    worktrees = _git_out(repo, "worktree", "list")
    held = push.take(p, "origin", "proj", 0, who="someone else")
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    try:
        assert _ttp("push", "--batch", str(marker)) == 75
    finally:
        held.close()
    m = push._read(marker)
    assert m["outcome"] == "busy" and _statuses(m) == ["requeued"] and "someone else" in m["message"], m
    assert _git_out(repo, "worktree", "list") == worktrees and not (p.worktrees / "push").exists()


def test_the_default_push_wait_outlasts_the_measured_check_run(env, monkeypatch):
    """The lock is held through the checks, so a waiter's default wait must cover them (measured
    150-535 s, longer than the old fixed 300 s); an explicit push_wait_s still wins."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["sleep 1"])
    from ttp import push
    assert push.last_check_s(p) is None
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert 1 <= push.last_check_s(p) < 30, "a passing check run must be measured"
    waits = []
    monkeypatch.setattr(push, "take", lambda p, remote, branch, wait_s, **kw: waits.append(wait_s))
    _commit(repo, "more.txt", "more\n")
    assert _ttp_push() == 75 and waits[-1] == push.DEFAULT_WAIT_S == 900
    push.record_check_s(p, 535)
    assert _ttp_push() == 75 and waits[-1] == 2 * 535 + push.WAIT_MARGIN_S > 535
    p.set_config("delivery.push_wait_s", 5)
    assert _ttp_push() == 75 and waits[-1] == 5


def test_the_measured_push_wait_is_bounded(env, monkeypatch):
    """A corrupt or huge timing must not make a waiter sit for hours or forever: Infinity, NaN and
    negative values are ignored, and the measured default is capped; an explicit push_wait_s is not."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp import push
    for raw in ('{"last_s": Infinity}', '{"last_s": NaN}', '{"last_s": -Infinity}', '{"last_s": -5}'):
        (p.state / push.TIMINGS).write_text(raw)
        assert push.last_check_s(p) is None, raw
    assert push.default_wait(None) == push.DEFAULT_WAIT_S
    assert push.default_wait(10 ** 9) == push.MAX_DEFAULT_WAIT_S == 2 * 3600
    push.record_check_s(p, 10 ** 6)
    waits = []
    monkeypatch.setattr(push, "take", lambda p, remote, branch, wait_s, **kw: waits.append(wait_s))
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 75 and waits[-1] == push.MAX_DEFAULT_WAIT_S
    p.set_config("delivery.push_wait_s", 5 * 3600)
    assert _ttp_push() == 75 and waits[-1] == 5 * 3600


def test_a_failed_timing_write_does_not_stop_the_push(env, monkeypatch, capsys):
    """Recording how long the checks took runs between the checks and the push: a full disk or a
    directory in the way is logged, and the push still goes out."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    from ttp import push
    (p.state / push.TIMINGS).mkdir(parents=True)
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert _git_out(repo, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", "refs/heads/proj")
    assert "could not record" in capsys.readouterr().err


def _pq_review(s, priority=3):
    """Another review of the same branch whose approval waits in the queue."""
    from ttp import pushq
    rid = s.p.db.add_task("another review", "r", kind="review", origin="coordinator", priority=priority)
    s.p.db.update_task(rid, status="pushing")
    with s.p.db.tx():
        [row] = pushq.approve(s.p, rid, None, {"entries": [{"branch": s.branch, "head": s.head}],
                                               "target": "origin/proj"})
    return rid, row


@pytest.mark.parametrize("link", ["pr_branch", "continues"])
def test_a_re_review_approves_the_head_its_re_plan_left_on_its_own_branch_in_one_run(env, monkeypatch, link):
    """The coordinator queues a re-review with the re-plan; it names only the reviewed branch, and the
    re-plan's head is on the re-plan's own branch. The push queue takes that head at once, with no
    second review run, but only a commit that builds on the reviewed branch, on the newest fix branch."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    fix, path, branch, head = _re_plan(s, f"pr_branch:{s.branch}" if link == "pr_branch" else f"continues:{s.review}")
    s.p.db.update_task(fix, status="done")
    again = s.p.db.add_task("Re-review (narrowed)", f"Re-review branch {s.branch} after its re-plan.", kind="review",
                            origin="coordinator")
    # Not a commit that leaves the reviewed branch out, even on the newest fix branch.
    _git_out(path, "checkout", "-q", "-b", "elsewhere", s.head + "~1")
    _commit(path, "stray.txt", "not on the reviewed branch\n")
    stray = _git_out(path, "rev-parse", "HEAD")
    _git_out(path, "checkout", "-q", branch)
    check = pushq.check_approval(s.p, s.p.db.task(again), [{"branch": branch, "head": stray}])
    assert "none of the reviewed refs" in check["invalid"] and branch in check["invalid"]
    later = s.p.db.add_task("Re-plan again", "s", kind="code", origin="coordinator", labels=[f"pr_branch:{s.branch}"])
    s.p.db.update_task(later, branch="ttp/t%d-later" % later)
    _git_out(s.repo, "branch", f"ttp/t{later}-later", stray)
    assert "invalid" in pushq.check_approval(s.p, s.p.db.task(again), [{"branch": branch, "head": stray}])
    s.p.db.update_task(later, status="cancelled")
    t = _pq_hand_off(env, s, push=[{"branch": branch, "head": head}], task=again)
    assert t["status"] == "pushing" and "woke" not in json.loads(t["result"])
    [row] = s.p.db.q("SELECT * FROM push_queue")
    assert (row["task"], row["head"], row["status"]) == (again, head, "approved")


def test_a_batch_is_due_after_the_window_at_the_cap_for_priority_one_or_with_no_review_coming(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_batch_s=900, push_batch_max=3)
    s.p.db.update_task(s.review, status="running")   # a review is still at work: approvals may wait for it
    now = time.time()
    first = [_pq_review(s)[1], _pq_review(s)[1]]
    rows, why = pushq.due(s.p, now)
    assert rows == [] and "waiting" in why
    assert [(r["id"], r["branch"], r["target"], r["tries"]) for r in pushq.summary(s.p)["approved"]] == [
        (first[0], s.branch, "origin/proj", 0), (first[1], s.branch, "origin/proj", 0)]
    rows, why = pushq.due(s.p, now + 901)
    assert [r["id"] for r in rows] == first and "waited" in why
    later = [_pq_review(s)[1], _pq_review(s)[1]]
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == first + later[:1] and "approvals" in why, "push_batch_max starts and caps a batch"
    s.p.db.x("DELETE FROM push_queue WHERE id IN (?,?,?)", (*first, later[0]))
    urgent = _pq_review(s, priority=1)[1]
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == [urgent, later[1]] and "priority-1" in why, "the best priority goes first"
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (urgent,))
    assert pushq.due(s.p, now)[0] == []
    s.p.db.update_task(s.review, status="done")
    rows, why = pushq.due(s.p, now)
    assert [r["id"] for r in rows] == [later[1]] and "no review" in why


def test_a_batch_waits_push_min_gap_s_after_the_last_one_ended_unless_full_or_priority_one(env, monkeypatch):
    from ttp import project, pushq
    s = _pq(env, monkeypatch, push_batch_s=0, push_batch_max=3)
    rows = [_pq_review(s)[1]]
    now = time.time() + 1   # past every approval's creation: push_batch_s=0 starts at once
    assert pushq.due(s.p, now)[0], "no batch ran before: nothing to wait for"
    # The last batch died before writing an outcome (a reboot): finalize recorded when it ended.
    s.p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,after_finalized,outcome) "
             "VALUES('b1','m','origin/proj',?,?,?,?,'died')", (now - 4000, now - 600, now - 600, now - 600))
    got, why = pushq.due(s.p, now)
    assert got == [] and "1200 s left of the 1800 s gap" in why, "the default gap counts from the recorded end"
    assert pushq.schedule(s.p) is None
    assert [r["id"] for r in pushq.due(s.p, now + 1200)[0]] == rows, "the wait is bounded by the gap"
    assert [r["id"] for r in pushq.due(s.p, now + 10 ** 6)[0]] == rows
    rows.append(_pq_review(s, priority=1)[1])
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == rows[::-1] and "priority-1" in why, "an urgent approval starts at once"
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (rows.pop(),))
    rows += [_pq_review(s)[1], _pq_review(s)[1]]
    got, why = pushq.due(s.p, now)
    assert [r["id"] for r in got] == rows and "3 approvals" in why, "push_batch_max still forces a start"
    s.p.set_config("delivery.push_min_gap_s", 0)
    s.p.db.x("DELETE FROM push_queue WHERE id=?", (rows.pop(),))
    assert pushq.due(s.p, now)[0], "0 turns the gap off"
    s.p.set_config("delivery.push_min_gap_s", 300)
    assert pushq.due(s.p, now)[0], "the gap is configurable"
    s.p.db.x("UPDATE push_batches SET ended=?", (now + 3600,))
    assert pushq.due(s.p, now)[0], "an end in the future (the clock went back) never holds the queue"
    assert project.push_queue_number("push_min_gap_s", "0") == 0
    with pytest.raises(ValueError, match="push_min_gap_s.*default 1800"):
        project.push_queue_number("push_min_gap_s", -1)


@pytest.mark.parametrize("blocker", ["paused branch", "paused push", "live batch", "busy branch", "detached push",
                                     "backoff", "other target", "not allowed"])
def test_no_batch_starts_while_something_holds_the_push(env, monkeypatch, blocker):
    from ttp import locks, push, pushq
    from ttp.db import PAUSED_RESOURCES_KEY
    s = _pq(env, monkeypatch)
    rid, row = _pq_review(s, priority=1)
    now = time.time()
    assert pushq.due(s.p, now)[0], "the priority-1 approval alone is due"
    held = None
    if blocker.startswith("paused"):
        name = push.lock_name("origin", "proj") if blocker == "paused branch" else "push"
        s.p.db.set_kv(PAUSED_RESOURCES_KEY, {name: {"reason": "user", "since": now, "by": "user"}})
    elif blocker == "live batch":   # its deploy included
        s.p.db.x("INSERT INTO push_batches(id,marker,target,started,finalized) VALUES('b','m','origin/proj',?,?)",
                 (now, now))
    elif blocker == "busy branch":
        held = locks.try_take(push.lock_paths(s.p, "origin", "proj"), "someone's ttp push")
    elif blocker == "detached push":
        held = locks.try_take([s.p.state / "locks" / "push:run-123.0.lock"], "a detached ttp push")
    elif blocker == "backoff":
        s.p.db.set_kv(pushq.KV, {"backoff_until": now + 60})
    elif blocker == "other target":
        s.p.set_config("delivery.push_branch", "origin/other")
    else:
        s.p.set_config("delivery.push_allowed", False)
    try:
        rows, why = pushq.due(s.p, now)
        assert rows == [], why
        assert pushq.schedule(s.p) is None and not s.p.db.q("SELECT id FROM push_batches WHERE id!='b'")
    finally:
        if held:
            held.close()
    if blocker == "other target":   # approvals for the old target go back to their reviews
        t = s.p.db.task(rid)
        assert t["status"] == "queued" and json.loads(t["result"])["woke"].startswith(
            "push target changed: approved for origin/proj, the target is now origin/other")
        assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row,))["status"] == "cancelled"


def test_a_due_batch_starts_one_detached_process_that_holds_its_run_lock(env, monkeypatch):
    from ttp import locks, pushq
    s = _pq(env, monkeypatch)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), sha="ab" * 20, version="1.2.3")
    _pq_hand_off(env, s)
    bid = pushq.schedule(s.p)   # no review is running or ready: due at once
    assert bid
    child = pushq._children[bid]
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    assert (m["kind"], m["status"], m["phase"], m["target"], m["repo"], m["pid"]) == (
        "batch", "running", "push", "origin/proj", str(s.p.root), child.pid)
    [e] = m["entries"]
    assert (e["task"], e["branch"], e["head"], e["ref"]) == (s.review, s.branch, s.head, f"refs/ttp/push/{e['id']}")
    assert m["lock"] == str(s.p.state / "locks" / f"push:run-{bid}.0.lock")
    assert not locks.any_free([pathlib.Path(m["lock"])]), "the batch does not hold its run lock"
    assert os.getsid(child.pid) == child.pid, "the batch should run in a session of its own"
    assert s.p.db.one("SELECT status, batch FROM push_queue") == {"status": "batched", "batch": bid}
    assert s.p.db.one("SELECT target, finalized FROM push_batches") == {"target": "origin/proj", "finalized": None}
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == [], "one batch at a time"
    assert pushq.finalize(s.p) == [] and s.p.db.task(s.review)["status"] == "pushing"
    sm = pushq.summary(s.p)
    assert sm["on"] and sm["approved"] == [] and sm["last"] == []
    assert (sm["live"]["id"], sm["live"]["phase"], sm["live"]["rows"], sm["live"]["target"]) == (bid, "push", 1,
                                                                                               "origin/proj")
    release.write_text("")
    child.wait(timeout=30)
    assert locks.any_free([pathlib.Path(m["lock"])])


def test_a_pushed_batch_closes_the_review_done_without_any_model_run(env, monkeypatch):
    from ttp import pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    sha = "ab" * 20
    _pq_plan(s, outcome="pushed", sha=sha, tip="cd" * 20, version="0.3.1")
    _pq_hand_off(env, s)
    mark, runs = _pq_mark(s.p), s.p.db.one("SELECT COUNT(*) n FROM runs")["n"]
    s.p.db.set_kv("last_coordinator_turn", time.time())   # no idle wake while it runs: that is not the queue's
    d = Daemon(s.p.base)
    # the review closes before the batch's after-push step settles: wait for both
    assert _run_until(d, s.p, lambda: s.p.db.task(s.review)["status"] == "done" and not s.p.db.one(
        "SELECT id FROM push_batches WHERE after_finalized IS NULL"), timeout=60)
    assert s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == runs, "the push cycle started a model run"
    t = s.p.db.task(s.review)
    res = json.loads(t["result"])
    [bid] = [b["id"] for b in s.p.db.q("SELECT id FROM push_batches")]
    assert res["pushed"] == [{"branch": s.branch, "head": s.head, "sha": sha, "version": "0.3.1", "batch": bid,
                              "status": "pushed"}]
    assert res["summary"] == f"looks good (pushed {sha[:7]} as 0.3.1 to origin/proj)" and res["status"] == "done"
    assert s.p.db.one("SELECT status, pushed_sha, version FROM push_queue") == {
        "status": "pushed", "pushed_sha": sha, "version": "0.3.1"}
    evs = _pq_events(s.p, mark)
    assert [e["status"] for e in evs if e["kind"] == "task_done"] == ["handled"]
    assert not [e for e in evs if e["status"] == "queued"], "a pushed review woke the coordinator"
    assert subprocess.run(["git", "-C", str(s.repo), "show-ref", "--verify", "--quiet", "refs/ttp/push/1"]).returncode, \
        "the pin of a pushed row of a finished task stays"
    sm = pushq.summary(s.p)
    assert sm["live"] is None and sm["approved"] == [] and sm["deaths"] == 0
    assert [(b["id"], b["outcome"], b["pushed_sha"], b["version"], b["after_push"]) for b in sm["last"]] == [
        (bid, "pushed", sha, "0.3.1", "skipped")]
    state = lambda: (s.p.db.q("SELECT * FROM push_queue"), s.p.db.q("SELECT * FROM push_batches"),  # noqa: E731
                     s.p.db.q("SELECT id FROM events"), s.p.db.task(s.review))
    before = state()
    assert pushq.finalize(s.p) == [] and pushq.tend(s.p) == [] and state() == before, "a second finalize changed things"


@pytest.mark.parametrize("case", ["off", "empty", "approved", "running", "deploying", "finished", "failed"])
def test_status_shows_one_push_queue_line_in_each_state_and_never_a_command(env, case):
    from ttp import pushq
    from ttp.cli import status_text
    p, rid = _pq_rows(env, on=case != "off")
    now = time.time()
    sha = "cd" * 20
    if case in ("approved", "running", "failed"):
        _pq_row(p, rid, now, age=361)
        _pq_row(p, rid, now, age=60, head="ef" * 20)
    if case == "running":
        _pq_batch_row(p, "b2", now, age=125, phase="push")
        _pq_row(p, rid, now, status="batched", batch="b2", age=200)
        _pq_row(p, rid, now, status="batched", batch="b2", age=150)
    if case in ("deploying", "finished", "failed"):
        _pq_batch_row(p, "b1", now, age=900, outcome="pushed", pushed_sha=sha, version="0.2.140", ended=now - 720,
                      finalized=now - 715, check_runs=1, check_s=242.4,
                      **({"after_push": "ok", "after_finalized": now - 700} if case != "deploying" else {}))
        if case == "deploying":   # the push phase is over (finalized); after_push still runs
            (p.state / "pushes" / "b1.json").write_text(json.dumps({"id": "b1", "phase": "after_push"}))
            _pq_row(p, rid, now, status="pushed", batch="b1", age=1000, sha=sha, version="0.2.140")
    if case == "failed":
        _pq_batch_row(p, "b3", now, age=400, outcome="tip_failed", tip="aa" * 20, ended=now - 300,
                      finalized=now - 290, after_push="skipped", after_finalized=now - 290, check_runs=1, check_s=30)
    line = pushq.status_line(p, now)
    want = {
        "off": None,
        "empty": "push queue: empty",
        "approved": "push queue: 2 approved (oldest 6 min)",
        "running": "push queue: 2 approved (oldest 6 min) · batch checking since 2 min (2 changes)",
        "deploying": f"push queue: batch deploying since 15 min (1 change) · last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago",
        "finished": f"push queue: empty · last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy ok",
        "failed": f"push queue: 2 approved (oldest 6 min) · last batch the branch tip fails its checks 5 min ago · "
                  f"last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy ok",
    }[case]
    assert line == want
    assert [ln for ln in status_text(p).splitlines() if ln.startswith("push queue")] == ([want] if want else [])
    if want:
        assert "ttp " not in line and "`" not in line and " run " not in line, "the status line tells the user to act"


def test_the_push_queue_line_stays_once_the_queue_is_off_while_it_has_rows(env):
    from ttp import pushq
    p, rid = _pq_rows(env, on=False)
    now = time.time()
    _pq_row(p, rid, now, age=60)
    assert pushq.status_line(p, now) == "push queue (off): 1 approved (oldest 1 min)"


def test_a_push_notice_names_the_branch_and_sha_and_says_when_the_work_is_not_yet_on_its_intended_target(
        env, monkeypatch, capsys):
    """'pushed' must never read as landed: with delivery.base_ref naming another branch than the one
    pushed, `ttp push` names the branch and short sha and says the work is not on that branch yet,
    how far behind it is, and never assumes a branch it cannot read was reached."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    assert push.intended(p, repo, "origin/proj") == "", "the push branch itself names no other target"
    assert push.reach(p, repo, "origin/proj", _git_out(repo, "rev-parse", "HEAD")) is None
    for same in ("proj", "origin/proj", "refs/remotes/origin/proj", "refs/heads/proj", "ab" * 20):
        p.set_config("delivery.base_ref", same)
        assert push.intended(p, repo, "origin/proj") == "", same
    p.set_config("delivery.base_ref", "origin/main")
    _commit(repo, "a.txt", "a\n")
    _commit(repo, "b.txt", "b\n")
    capsys.readouterr()
    assert _ttp_push() == 0
    head = _git_out(origin, "rev-parse", "proj")
    err = capsys.readouterr().err
    assert f"pushed {head[:7]} to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in err, err
    assert push.last_reach == {"ref": "origin/main", "on": False, "behind": 2}
    # --own publishes another branch: the push branch is what the work is meant to reach.
    assert push.intended(p, repo, "origin/ttp/t7-exp") == "origin/proj"
    # The target catches up: the same push is on it too.
    _git_out(repo, "push", "-q", "origin", f"{head}:refs/heads/main")
    r = push.reach(p, repo, "origin/proj", head)
    assert r == {"ref": "origin/main", "on": True, "behind": 0}
    assert push.landing(head, "origin/proj", r, "0.1.1") == f"pushed {head[:7]} as 0.1.1 to origin/proj, also on origin/main"
    # A target that cannot be read is never taken as reached.
    p.set_config("delivery.base_ref", "origin/gone")
    assert push.landing(head, "origin/proj", push.reach(p, repo, "origin/proj", head)) == (
        f"pushed {head[:7]} to origin/proj, not yet seen on origin/gone (it could not be read)")
    # The detached push's verdict says it as well.
    marker = p.state / "pushes" / "x.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": "x", "status": "pushed", "exit": 0, "sha": head, "target": "origin/proj",
                                  "reach": {"ref": "origin/main", "on": False, "behind": 1}}))
    capsys.readouterr()
    assert push.result(marker) == 0
    assert capsys.readouterr().out.strip() == (
        f"ttp push: pushed {head} to origin/proj, not yet on origin/main (origin/main is 1 commit behind)")


def test_a_push_batch_records_whether_what_it_pushed_is_on_the_intended_target(env, monkeypatch, capsys):
    """A batch to the push branch, with delivery.base_ref naming another branch, records in its marker
    that the pushed commit is not on that branch yet, and its result says so; with no other branch
    named it records nothing."""
    from ttp import batch
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    h1 = _entry(repo, "e1", {"one.txt": "1\n"})
    rc, m = _run_batch(p, _batch_marker(p, [h1]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"] is None, m
    p.set_config("delivery.base_ref", "origin/main")
    h2 = _entry(repo, "e2", {"two.txt": "2\n"})
    capsys.readouterr()
    rc, m = _run_batch(p, _batch_marker(p, [h2], bid="b2"), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert m["reach"] == {"ref": "origin/main", "on": False, "behind": 2}, m
    sha = _git_out(origin, "rev-parse", "proj")
    out = capsys.readouterr().out
    assert f"pushed {sha[:7]} to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in out, out
    assert batch.summary(p.state / "pushes" / "b2.json") == 0
    out = capsys.readouterr().out
    assert f"to origin/proj, not yet on origin/main (origin/main is 2 commits behind)" in out.splitlines()[0], out


def test_a_reach_check_that_times_out_or_raises_never_changes_a_push_that_landed(env, monkeypatch, capsys):
    """The reach check runs after the push landed: its fetch is bounded, prompt-free and stdin-free, a
    timeout reads as 'could not be read', and an exception leaves `ttp push` at 0 and a batch's
    marker at outcome 'pushed' with no reach."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    p.set_config("delivery.base_ref", "origin/main")
    real, seen = subprocess.run, []

    def stalled(cmd, *a, **kw):
        if "fetch" in cmd and "refs/remotes/origin/main" in cmd[-1]:
            seen.append(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(push.subprocess, "run", stalled)
    _commit(repo, "a.txt", "a\n")
    capsys.readouterr()
    assert _ttp_push() == 0
    assert push.last_reach == {"ref": "origin/main", "on": None, "behind": None}
    assert "not yet seen on origin/main (it could not be read)" in capsys.readouterr().err
    kw = seen[0]
    assert kw["timeout"] == push.REACH_FETCH_S <= 60 and kw["stdin"] == subprocess.DEVNULL, kw
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0", kw
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"one.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"]["on"] is None, m
    monkeypatch.setattr(push.subprocess, "run", real)

    def broken(*a, **kw):
        raise RuntimeError("no git")
    monkeypatch.setattr(push, "reach", broken)
    _commit(repo, "b.txt", "b\n")
    capsys.readouterr()
    assert _ttp_push() == 0 and push.last_reach is None
    assert "cannot tell whether it is on the branch it is meant to reach: RuntimeError" in capsys.readouterr().err
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"two.txt": "2\n"})], bid="b2"), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed" and m["reach"] is None, m


def test_a_pushed_batch_tells_the_review_status_and_web_app_it_is_not_yet_on_the_intended_target(env, monkeypatch):
    """The review's summary, its done event, `ttp status`'s queue line, `ttp push --queue` and the web
    app's batch row all name the branch and short sha and say the work is not yet on the branch it is
    meant to reach, instead of letting 'pushed' read as landed."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    sha = "ab" * 20
    reach = {"ref": "origin/main", "on": False, "behind": 4}
    _pq_plan(s, outcome="pushed", sha=sha, tip="cd" * 20, version="0.3.1", reach=reach)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    words = f"pushed {sha[:7]} as 0.3.1 to origin/proj, not yet on origin/main (origin/main is 4 commits behind)"
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["summary"] == f"looks good ({words})"
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "task_done"]
    assert f"({words}, batch " in ev["text"], ev
    now = time.time()
    sm = pushq.summary(s.p, now)
    assert sm["last"][0]["landing"] == words and sm["last_pushed"]["reach"] == reach
    line = pushq.status_line(s.p, now, sm)
    assert f"last pushed {sha[:7]} as 0.3.1 to origin/proj " in line and \
        "ago, not yet on origin/main (origin/main is 4 commits behind)" in line, line
    assert any(ln.endswith("ago: " + words + ", checks 1 run in 2 s, no deploy") for ln in
               pushq.queue_text(s.p, now).splitlines()), pushq.queue_text(s.p, now)
    assert pushq.web(s.p, now=now)["last"][0]["landing"] == words


def test_a_batch_records_its_reach_with_its_outcome_so_a_tick_meanwhile_cannot_settle_without_it(env, monkeypatch):
    """finalize settles a marker as soon as it has an outcome, even while the batch runs: the reach
    check runs before the outcome is written, so a tick during its fetch finds nothing to settle and
    the review's one-shot summary still says the work is not yet on the intended target."""
    from ttp import batch, push, pushq
    s = _pq(env, monkeypatch)
    _git_out(s.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    s.p.set_config("delivery.base_ref", "origin/main")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, "-c", "pass"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = pathlib.Path(s.p.db.one("SELECT marker FROM push_batches WHERE id=?", (bid,))["marker"])
    real, seen = push.reach, []

    def reach(*a, **kw):
        seen.append(push._read(marker).get("outcome"))
        _pq_tend(s)                 # a daemon tick while the fetch runs
        seen.append(s.p.db.task(s.review)["status"])
        return real(*a, **kw)
    monkeypatch.setattr(push, "reach", reach)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("TTP_RUN_DIR", push._read(marker)["check_report_dir"])
    assert batch.run_batch(s.p, marker) == 0
    assert seen == [None, "pushing"], seen
    m = push._read(marker)
    assert m["outcome"] == "pushed" and m["reach"]["ref"] == "origin/main" and m["reach"]["on"] is False, m
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and "not yet on origin/main" in json.loads(t["result"])["summary"], t


def test_ttp_push_queue_lists_the_entries_and_the_last_ten_batches(env, monkeypatch, capsys):
    from ttp import cli, pushq
    p, rid = _pq_rows(env)
    now = time.time()
    sha = "cd" * 20
    for i in range(12):
        _pq_batch_row(p, f"b{i:02d}", now, age=7200 - i * 60, outcome="nothing", ended=now - 7000 + i * 60,
                      finalized=now, after_push="skipped", after_finalized=now)
    _pq_batch_row(p, "b20", now, age=900, outcome="pushed", pushed_sha=sha, version="0.2.140", ended=now - 720,
                  finalized=now, check_runs=2, check_s=242.4, after_push="failed", after_finalized=now)
    _pq_row(p, rid, now, status="pushed", batch="b20", age=1000, sha=sha, version="0.2.140")
    _pq_row(p, rid, now, age=361, head="ef" * 20)
    _pq_batch_row(p, "b21", now, age=30, phase="push")
    _pq_row(p, rid, now, status="batched", batch="b21", age=100, head="12" * 20)
    out = pushq.queue_text(p, now).splitlines()
    assert out[0].startswith("push queue: 1 approved (oldest 6 min) · batch checking since 0 min (1 change) · "
                             f"last pushed {sha[:7]} as 0.2.140 to origin/proj 12 min ago · deploy failed"), out[0]
    i = out.index("entries:")
    assert out[i + 1:i + 4] == [
        f"  #{rid} ttp/t1-feature 1212121 batched in b21, 1 min old: review feature",
        f"  #{rid} ttp/t1-feature efefefe approved, 6 min old: review feature",
        f"  #{rid} ttp/t1-feature abababa pushed in b20, 16 min old: review feature"]
    j = out.index("batches, newest first:")
    assert out[j + 1] == "  b21 running: checking since 0 min, 1 change"
    assert out[j + 2] == f"  b20 12 min ago: pushed {sha[:7]} as 0.2.140 to origin/proj, checks 2 runs in 242 s, deploy failed"
    assert out[j + 3].startswith("  b11 ") and out[j + 3].endswith("ago: nothing to push, no deploy")
    assert len(out) - j - 2 == 10, "the last 10 batches, plus the live one"
    # the CLI flag prints the same, from the project's folder, and pushes nothing
    monkeypatch.chdir(p.root)
    monkeypatch.setattr(sys, "argv", ["ttp", "push", "--queue"])
    cli.main()
    printed = capsys.readouterr().out
    assert "entries:" in printed and "b20 12 min ago: pushed" in printed


def test_a_conflicting_head_gets_no_rebase_review_past_the_review_cap_or_after_one_already_ran(env, monkeypatch):
    """The rolling review-task cap holds the automatic rebase back: the review fails to the coordinator
    as before. A head that already had a rebase review gets none again, whoever re-approves it."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    s.p.set_config("coordinator.max_review_tasks_per_day", 0)
    s.p.db.update_task(s.review, max_attempts=1)
    row = {"head": s.head, "branch": s.branch}
    with s.p.db.tx():
        assert pushq.auto_rebase(s.p, s.p.db.task(s.review), row, "origin/proj", "push conflict: x") is None
    s.p.set_config("coordinator.max_review_tasks_per_day", 50)
    other = s.p.db.add_task("Review again", "r", kind="review", origin="coordinator", depends_on=[s.code])
    with s.p.db.tx():
        first = pushq.auto_rebase(s.p, s.p.db.task(s.review), row, "origin/proj", "push conflict: x")
        assert first and pushq.auto_rebase(s.p, s.p.db.task(other), row, "origin/proj", "push conflict: x") is None
    assert pushq.rebase_head(s.p.db.task(first)) == s.head


def test_the_daemon_closes_code_tasks_left_in_review_whose_latest_review_pushed_them(env):
    """Clean-up of tasks left behind: only a task whose latest covering review is done and pushed its
    work after the task entered review is closed, once."""
    from ttp import pushq
    from ttp.daemon import Daemon
    p, _ = _pq_rows(env)
    db, now = p.db, time.time()

    def code(name, at):
        tid = db.add_task(name, "s", kind="code", tier="light", origin="user")
        db.update_task(tid, status="review", branch=f"ttp/t{tid}-{name}")
        db.x("UPDATE tasks SET updated=? WHERE id=?", (at, tid))
        return tid

    def review(title, status, row=None, at=now, **kw):
        rid = db.add_task(title, "r", kind="review", origin="coordinator", **kw)
        db.update_task(rid, status=status)
        if row:
            db.x("INSERT INTO push_queue(task, branch, head, target, status, batch, created, updated, pushed_sha, "
                 "version) VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, row, "ab" * 20, "origin/proj", "pushed", "b1",
                                                          at - 60, at, "cd" * 20, "0.3.2"))
        return rid

    shipped, by_branch, by_dep = code("shipped", now - 500), code("branch", now - 500), code("dep", now - 500)
    reopened, pending, unreviewed = code("reopened", now - 100), code("pending", now - 500), code("none", now - 500)
    review(f"Review #{shipped}: x", "failed")                          # an earlier round, failed on a conflict
    done = review(f"Review #{shipped}: settle the conflict", "done", f"ttp/t{shipped}-shipped")
    review("land it", "done", f"ttp/t{by_branch}-branch")             # pushed the task's branch
    review("review the stack", "done", "ttp/other", depends_on=[by_dep])
    review(f"Review #{reopened}: x", "done", f"ttp/t{reopened}-reopened", at=now - 300)   # before it re-entered review
    review(f"Review #{pending}: x", "done", f"ttp/t{pending}-pending")
    review(f"Review #{pending}: again", "queued")                                          # a newer review still open
    Daemon(p.base).tend_pushes()
    st = {i: db.task(i)["status"] for i in (shipped, by_branch, by_dep, reopened, pending, unreviewed)}
    assert st == {shipped: "done", by_branch: "done", by_dep: "done", reopened: "review", pending: "review",
                  unreviewed: "review"}
    res = json.loads(db.task(shipped)["result"])
    assert res["shipped_by"] == done and res["pushed"][0]["sha"] == "cd" * 20 and res["pushed"][0]["version"] == "0.3.2"
    assert len(db.q("SELECT id FROM events WHERE kind='task_done'")) == 3
    assert pushq.settle_reviewed(db) == [] and len(db.q("SELECT id FROM events WHERE kind='task_done'")) == 3


def test_a_broken_target_tip_is_reported_once_and_held_until_the_tip_changes(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_min_gap_s=0)   # the hold alone, no gap
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    _pq_plan(s, outcome="tip_failed", tip=tip, row="requeued", message="the tip of origin/proj fails its checks",
             tip_check={"cmd": "make test", "tail": "the tip is red"})
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    for _ in range(2):   # the same tip fails twice: one report
        _pq_batch(s)
        _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "approved"
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_tip_failed" and "make test" in ev["text"] and "the tip is red" in ev["text"]
    rows, why = pushq.due(s.p, time.time() + 60)
    assert rows == [] and "held" in why
    rows, why = pushq.due(s.p, time.time() + pushq.HOLD_S + 60)
    assert rows == [] and "held" in why, "known bad after TIP_FAILS batches: held past HOLD_S"
    assert pushq.due(s.p, time.time() + pushq.BAD_TIP_RETRY_S + 60)[0], "the hold ends after BAD_TIP_RETRY_S"
    assert len(s.p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref=?", (pushq.BAD_TIP_ALERT,))) == 1
    _commit(s.other, "theirs.txt", "theirs\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    rows, why = pushq.due(s.p, time.time() + 60)
    assert rows, f"a new tip should end the hold: {why}"
    _pq_plan(s, outcome="tip_failed", tip=_git_out(s.repo, "rev-parse", "refs/remotes/origin/proj"), row="requeued",
             tip_check={"cmd": "make test", "tail": "still red"})
    _pq_batch(s)
    _pq_tend(s)
    assert [e["kind"] for e in _pq_events(s.p, mark) if e["status"] == "queued"] == ["push_tip_failed"] * 2


def test_a_tip_failing_only_where_the_queue_checks_it_stops_being_retried_and_a_fixing_approval_lands(
        env, monkeypatch):
    """Replays a push branch tip whose checks fail only in the batch's own checkout (a path-dependent
    failure: they passed in the task's worktree) while more approvals wait than a batch takes. After
    TIP_FAILS tip_failed batches on that tip, one keyed alert is raised and the approvals already tried
    on it wait, not run again and again. A new approval goes first; the tip with it passes, so it is
    pushed, which clears the alert, and the waiting approval then lands on the fixed tip."""
    from ttp import alerts, pushq
    check = 'case "$(pwd -P)" in */push) test -e fix.txt ;; esac'
    s = _pq_real(env, monkeypatch, push_min_gap_s=0, push_batch_max=1, push_checks=[check])
    db = s.p.db
    _pq_hand_off(env, s)
    tip = _git_out(s.origin, "rev-parse", "proj")
    log = lambda bid: (s.p.state / "pushes" / f"{bid}.log").read_text()
    for n in range(1, pushq.TIP_FAILS + 1):
        bid = _pq_batch(s)
        m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
        assert m["outcome"] == "tip_failed" and m["checks"]["runs"] == 2 and "bad_tip" not in m, log(bid)
        _pq_tend(s)
        assert (db.kv("push_queue")["bad_tip"]["fails"], db.task(s.review)["status"]) == (n, "pushing")
    alerted = lambda: db.q("SELECT text FROM messages WHERE kind='alert' AND ref=?", (pushq.BAD_TIP_ALERT,))
    [a] = alerted()
    assert tip[:7] in a["text"] and check in a["text"]
    assert alerts.holds(db, pushq.BAD_TIP_ALERT, time.time(), time.time())
    rows, why = pushq.due(s.p, time.time() + pushq.HOLD_S + 60)
    assert rows == [] and "held" in why, "the tried approval is not checked on that tip again"
    code, path, branch = _code_task(s.p, "fix")
    _commit(path, "fix.txt", "fix\n")
    fix = db.add_task("review fix", "review the fix", kind="review", tier="standard", origin="coordinator",
                      depends_on=[code])
    _pq_hand_off(env, s, task=fix, push=[{"branch": branch, "head": _git_out(path, "rev-parse", "HEAD")}])
    rows, why = pushq.due(s.p, time.time() + 60)
    assert [r["task"] for r in rows] == [fix], why
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["bad_tip"] == {"tip": tip, "fresh": 1, "cmd": check}, m
    assert m["outcome"] == "pushed" and m["checks"]["runs"] == 1, log(bid)
    _pq_tend(s)
    assert db.task(fix)["status"] == "done" and _git_out(s.origin, "show", "proj:fix.txt") == "fix"
    assert "bad_tip" not in db.kv("push_queue") and not alerts.holds(db, pushq.BAD_TIP_ALERT, 0, time.time())
    bid = _pq_batch(s)
    _pq_tend(s)
    assert db.task(s.review)["status"] == "done", log(bid)
    assert len(alerted()) == 1


def test_a_fix_behind_an_entry_that_does_not_fix_a_known_failing_tip_lands_in_the_next_batch(env, monkeypatch):
    """On a known-bad tip, a batch [e1, fix, bad] fails at prefix 1 and ends tip_failed. Only e1 is then
    tried on that tip: fix and bad stay untried, go first in the next batch at once (not held), and that
    batch pushes fix and blames bad. e1 then lands on the fixed tip."""
    from ttp import pushq
    s = _pq_real(env, monkeypatch, push_min_gap_s=0, push_batch_max=3,
                 push_checks=["test ! -e broken.txt || test -e fix.txt", "test ! -e bad.txt"])
    db = s.p.db
    _commit(s.other, "broken.txt", "x\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    tip = _git_out(s.origin, "rev-parse", "proj")
    db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": pushq.TIP_FAILS, "tried": [], "at": time.time(),
                                         "alerted": True}})
    _pq_hand_off(env, s)
    tasks = {}
    for name in ("fix", "bad"):
        code, path, branch = _code_task(s.p, name)
        _commit(path, f"{name}.txt", f"{name}\n")
        tasks[name] = db.add_task(f"review {name}", "r", kind="review", tier="standard", origin="coordinator",
                                  depends_on=[code])
        _pq_hand_off(env, s, task=tasks[name], push=[{"branch": branch, "head": _git_out(path, "rev-parse", "HEAD")}])
    row = {r["task"]: r["id"] for r in db.q("SELECT id, task FROM push_queue")}
    log = lambda bid: (s.p.state / "pushes" / f"{bid}.log").read_text()
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["outcome"] == "tip_failed" and m["tip_tried"] == [row[s.review]], log(bid)
    _pq_tend(s)
    assert db.kv("push_queue")["bad_tip"]["tried"] == [row[s.review]]
    rows, why = pushq.due(s.p, time.time() + 60)
    assert [r["task"] for r in rows] == [tasks["fix"], tasks["bad"], s.review], why
    bid = _pq_batch(s)
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert m["outcome"] == "pushed" and _statuses(m) == ["pushed", "check_failed", "requeued"], log(bid)
    _pq_tend(s)
    assert _git_out(s.origin, "show", "proj:fix.txt") == "fix" and "bad_tip" not in db.kv("push_queue")
    assert db.task(tasks["fix"])["status"] == "done" and db.task(tasks["bad"])["status"] != "done"
    bid = _pq_batch(s)
    _pq_tend(s)
    assert db.task(s.review)["status"] == "done", log(bid)


def test_a_failing_push_tip_is_forgotten_once_the_branch_moves(env, monkeypatch):
    """The known-bad tip and its alert end when the branch moves past it, even with no batch to run."""
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    s.p.db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": 2, "tried": [], "at": time.time(), "alerted": True}})
    pushq.schedule(s.p)
    assert alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time()), "same tip: still failing"
    _commit(s.other, "theirs.txt", "theirs\n")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    pushq.schedule(s.p)
    assert "bad_tip" not in s.p.db.kv("push_queue")
    assert not alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time())


@pytest.mark.parametrize("outcome, row, detail, passed, cleared", [
    ("nothing", "landed", {}, False, False),                             # its only entry is already on the tip
    ("landed", "landed", {}, False, False),
    ("nothing", "check_failed", {"cmd": "(stale plugin version)"}, False, False),   # the version guard
    ("nothing", "check_failed", {"cmd": "make test"}, True, True),       # the tip passed alone; the entry failed
    ("pushed", "pushed", {}, False, True),
])
def test_a_known_failing_push_tip_is_kept_until_a_batch_shows_it_passing(env, monkeypatch, outcome, row, detail,
                                                                       passed, cleared):
    """A batch on the known-bad tip that never checked it (nothing carried, or the version guard blamed
    the first entry) keeps the tip known bad and its alert up; the next batches do not check it alone."""
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    tip = _git_out(s.repo, "rev-parse", "refs/remotes/origin/proj")
    s.p.db.set_kv("push_queue", {"bad_tip": {"tip": tip, "fails": 2, "tried": [], "at": time.time(), "alerted": True}})
    _pq_plan(s, outcome=outcome, tip=tip, row=row, detail=detail, tip_passed=passed,
             sha="ab" * 20 if outcome == "pushed" else None)
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    st = s.p.db.kv("push_queue")
    assert ("bad_tip" not in st) == cleared, st
    assert alerts.holds(s.p.db, pushq.BAD_TIP_ALERT, time.time(), time.time()) != cleared
    if not cleared:
        assert st["bad_tip"]["tip"] == tip and st["bad_tip"]["fails"] == 2


@pytest.mark.parametrize("outcome, tries", [("moved", 1), ("busy", 0)])
def test_a_batch_that_could_not_push_puts_its_approvals_back_without_a_model_run(env, monkeypatch, outcome, tries):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome=outcome, row="requeued")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": tries}
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert not [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert pushq.due(s.p, time.time() + 10 ** 6)[0], "it goes out with the next batch"


def test_dead_batches_retry_with_a_growing_pause_and_three_in_a_row_raise_an_alert(env, monkeypatch):
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    for n in (1, 2, 3):
        before = time.time()
        _pq_batch(s)
        _pq_tend(s)
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": n}
        st = s.p.db.kv(pushq.KV)
        assert st["deaths"] == n and st["backoff_until"] >= before + min(1800, 300 * n)
        assert pushq.due(s.p, time.time())[0] == []
        assert not list((s.p.state / "locks").glob("push:run-*")), "a dead batch's run lock file stays"
        sent = s.p.db.q("SELECT severity FROM messages WHERE kind='alert' AND ref='push_queue_dying'")
        assert len(sent) == (1 if n == 3 else 0)
    assert [b["outcome"] for b in s.p.db.q("SELECT outcome FROM push_batches")] == ["died"] * 3
    assert not [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert s.p.db.task(s.review)["status"] == "pushing" and s.p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0
    assert alerts.holds(s.p.db, "push_queue_dying", before, time.time())
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done"
    assert [ep["key"] for ep in alerts.sweep(s.p.db)] == ["push_queue_dying"], "a finished batch clears the alert"


def _pq_boot(p, ts):
    """Record a host boot at `ts`, as the daemon does once it is back (Daemon._tell_reboot)."""
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
           (ts, "host", "boot", f"boot:{ts}", "normal", "the host rebooted (abrupt)", json.dumps({"boot_time": ts}),
            "record"))


@pytest.mark.parametrize("boot", ["inside", "before", "none"])
def test_a_batch_cut_short_by_a_reboot_runs_again_at_once_without_backoff(env, monkeypatch, boot):
    """A boot inside the batch's run: 'cut short by the HH:MM reboot', no try counted, no backoff, no
    death toward push_queue_dying, and due() takes it again at once (no gap). A batch that died
    with no boot inside its run (a kill) keeps the old path."""
    from ttp import pushq, timefmt
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    if boot == "before":
        _pq_boot(s.p, time.time() - 60)
    bid = _pq_batch(s)
    started = s.p.db.one("SELECT started FROM push_batches WHERE id=?", (bid,))["started"]
    booted = time.time()   # after the batch started, before the daemon found it dead
    if boot == "inside":
        _pq_boot(s.p, booted)
    _pq_tend(s)
    st = s.p.db.kv(pushq.KV) or {}
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "push_batch_died"]
    outcome = s.p.db.one("SELECT outcome FROM push_batches WHERE id=?", (bid,))["outcome"]
    if boot == "inside":
        assert f"cut short by the {timefmt.short(booted, s.p)} reboot" in ev["text"] and "at once" in ev["text"]
        assert ev["status"] == "handled" and outcome == "rebooted"
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
        assert not st.get("deaths") and not st.get("backoff_until")
        rows, why = pushq.due(s.p, time.time())
        assert rows and why == f"a rerun of {bid}, cut short by a reboot"
    else:
        assert "ended before writing an outcome" in ev["text"] and outcome == "died"
        assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
        assert st["deaths"] == 1 and st["backoff_until"] > time.time()
        assert pushq.due(s.p, time.time())[0] == []


def test_reboot_cut_batches_alone_raise_no_dying_alert(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    for _ in range(pushq.DYING_AFTER + 1):
        _pq_batch(s)
        _pq_boot(s.p, time.time())
        _pq_tend(s)
    assert not s.p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref='push_queue_dying'")
    assert s.p.db.one("SELECT tries FROM push_queue")["tries"] == 0


def test_nothing_landing_for_hours_alerts_once_with_counts_and_clears_on_landing(env, monkeypatch):
    from ttp import alerts, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_boot(s.p, time.time())
    _pq_tend(s)
    d = Daemon(s.p.base)
    later = time.time() + 5 * 3600
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later), "one batch is not yet 'despite retries'"
    _pq_batch(s)          # killed, no reboot
    _pq_tend(s)
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=time.time() + 3 * 3600), "under 4 h"
    assert pushq.check_landing(s.p, d.cfg, d.alert, now=later)
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later + 3600), "once per stall"
    [msg] = s.p.db.q("SELECT text, severity FROM messages WHERE kind='alert' AND ref=?", (pushq.LANDING_ALERT,))
    assert msg["severity"] == "high" and "Nothing has landed on origin/proj" in msg["text"]
    assert "2 push batches tried, 1 of them cut short by a host reboot" in msg["text"]
    assert alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert pushq.LANDING_ALERT in [ep["key"] for ep in alerts.sweep(s.p.db)], "a landing clears it"


def test_the_landing_alert_is_off_at_zero(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_landing_alert_h=0)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    for _ in range(3):
        _pq_batch(s)
        _pq_tend(s)
    assert not pushq.check_landing(s.p, now=time.time() + 10 ** 6)


def test_a_daemon_restart_mid_batch_finalizes_from_the_marker(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), outcome="pushed", sha="ab" * 20, version="1.0.1")
    _pq_hand_off(env, s)
    bid = _pq_batch(s, wait=False)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "pushing"
    child = pushq._children.pop(bid)   # the daemon that started it is gone
    release.write_text("")
    child.wait(timeout=30)
    _pq_tend(s)                        # a new daemon reads the marker
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["version"] == "1.0.1"


def test_the_push_queue_and_the_real_batch_process_agree_on_the_marker(env, monkeypatch):
    """The daemon's side (pushq) starts the real `ttp push --batch`, which pushes the approved head;
    finalize closes the review from the marker the batch wrote, and nothing of the batch is left."""
    from ttp import pushq
    s = _pq_real(env, monkeypatch)
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    assert pushq._children[bid].returncode == 0, (s.p.state / "pushes" / f"{bid}.log").read_text()
    m = json.loads((s.p.state / "pushes" / f"{bid}.json").read_text())
    assert (m["status"], m["phase"], m["outcome"], m["rounds"]) == ("finished", "finished", "pushed", 1), m
    assert [(r["task"], r["status"]) for r in m["results"]] == [(s.review, "pushed")]
    _pq_tend(s)
    sha = _git_out(s.origin, "rev-parse", "proj")
    assert _git_out(s.origin, "show", "proj:feature.txt") == "feature"
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    assert s.p.db.one("SELECT outcome, pushed_sha, after_push FROM push_batches") == {
        "outcome": "pushed", "pushed_sha": sha, "after_push": "skipped"}
    assert not list((s.p.state / "locks").glob(f"*run-{bid}*"))


def test_the_push_queue_resumes_a_cut_short_after_push_with_the_real_batch_process(env, monkeypatch):
    """A batch that died in after_push (a reboot) leaves its marker with an outcome, phase after_push
    and a free after_push:run lock. The queue starts the real batch once more under that same lock;
    it deploys the pushed commit only and finishes the marker."""
    from ttp import pushq
    deploys = env["tmp"] / "deploys.log"
    s = _pq_real(env, monkeypatch, after_push=[f"git rev-parse HEAD >> {deploys}"])
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    sha = m["pushed_sha"]
    assert m["after_push"]["status"] == "ok" and deploys.read_text().split() == [sha]
    # Cut short: as the batch left it once it handed over to its after_push lock.
    lock = s.p.state / "locks" / f"after_push:run-{bid}.0.lock"
    lock.write_text("")
    from ttp.project import write_json
    write_json(marker, {**{k: v for k, v in m.items() if k not in ("after_push", "ended")},
                        "status": "running", "phase": "after_push", "lock": str(lock)})
    _pq_tend(s)                    # applies the outcome, then runs the after_push once more
    assert s.p.db.task(s.review)["status"] == "done"
    child = pushq._children[bid]
    child.wait(timeout=60)
    assert child.returncode == 0, (s.p.state / "pushes" / f"{bid}.log").read_text()
    _pq_tend(s)
    m = json.loads(marker.read_text())
    assert (m["status"], m["phase"], m["after_push"]["status"]) == ("finished", "finished", "ok"), m
    assert deploys.read_text().split() == [sha, sha] and _git_out(s.origin, "rev-parse", "proj") == sha
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert not lock.exists()


def test_after_push_failures_are_reported_and_a_deploy_cut_short_runs_once_more(env, monkeypatch):
    from ttp import alerts
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_plan(s, outcome="pushed", sha="ab" * 20, after="failed")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done", "the review stays done whatever the deploy does"
    assert s.p.db.one("SELECT after_push FROM push_batches")["after_push"] == "failed"
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "after_push_failed" and "deploy said failed" in ev["text"]
    assert s.p.db.one("SELECT severity FROM messages WHERE kind='alert' AND ref='after_push_failed'")["severity"] == "high"
    # Cut short (a reboot) and cut short again when run once more: recorded as killed.
    rid, _ = _pq_review(s)
    _pq_plan(s, outcome="pushed", sha="cd" * 20, after="ok", die="after", die_resume=True)
    bid = _pq_batch(s)
    _pq_tend(s)   # applies the outcome and runs the after_push step once more
    assert s.p.db.task(rid)["status"] == "done"
    from ttp import pushq
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches WHERE id=?", (bid,)) == {
        "after_push": "killed", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    assert [e["kind"] for e in _pq_events(s.p, mark) if e["status"] == "queued"] == ["after_push_failed"] * 2
    # Cut short once, then it works: one handled event, and the alert clears.
    rid, _ = _pq_review(s)
    _pq_plan(s, outcome="pushed", sha="ef" * 20, after="ok", die="after", resume_after="ok")
    bid = _pq_batch(s)
    _pq_tend(s)
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push FROM push_batches WHERE id=?", (bid,))["after_push"] == "ok"
    assert [e["status"] for e in _pq_events(s.p, mark) if e["kind"] == "after_push_ok"] == ["handled"]
    assert "after_push_failed" in [ep["key"] for ep in alerts.sweep(s.p.db)]


def _pq_pushing(s, sha, row="pushed", at=None):
    """The plan of a batch that dies after it began pushing `sha`, before writing its outcome."""
    rows = s.p.db.q("SELECT id, task FROM push_queue WHERE status='approved'")
    _pq_plan(s, die="push", pushing={"sha": sha, "tip": _git_out(s.origin, "rev-parse", "proj"), "version": "1.0.2",
                                     "at": time.time() if at is None else at, "checks": {"runs": 1, "seconds": 2.0}, "rounds": 1,
                                     "results": [{"id": r["id"], "task": r["task"], "status": row, "sha": sha,
                                                  "detail": {}} for r in rows]})


def test_a_batch_that_died_after_its_push_landed_counts_as_pushed_and_runs_its_after_push(env, monkeypatch):
    """A reboot between the batch's `git push` and its outcome: the head it was pushing is on the
    branch, so the queue settles the batch as pushed from what it recorded before the push. Its
    approvals are not queued again (no second push), the review is done, and its after_push runs."""
    from ttp import pushq
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    sha = _git_out(s.origin, "rev-parse", "proj")   # what the dead batch pushed is the branch tip
    _pq_pushing(s, sha)
    bid = _pq_batch(s)
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    assert s.p.db.one("SELECT status, tries, landed_sha FROM push_queue") == {
        "status": "pushed", "tries": 0, "landed_sha": sha}
    assert s.p.db.one("SELECT outcome, pushed_sha, version FROM push_batches") == {
        "outcome": "pushed", "pushed_sha": sha, "version": "1.0.2"}
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == [], "nothing goes out again"
    assert (s.p.db.kv(pushq.KV) or {}).get("deaths", 0) == 0
    pushq._children[bid].wait(timeout=30)            # the after_push, run once more
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    evs = _pq_events(s.p, mark)
    assert [e["status"] for e in evs if e["kind"] == "push_batch_recovered"] == ["handled"]
    assert not [e for e in evs if e["status"] == "queued"], "recovered: the coordinator is not woken"
    assert not [e for e in evs if e["kind"] == "push_batch_died"]


def test_a_batch_that_died_before_its_push_landed_puts_each_approval_back_once(env, monkeypatch):
    """The dead batch had begun pushing, but its head is not on the branch: its approvals go back to
    the queue exactly once and are never called landed; the next batch pushes them."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_pushing(s, s.head)                          # the reviewed head was never pushed
    _pq_batch(s)
    _pq_tend(s)
    _pq_tend(s)                                     # a second tick changes nothing
    assert s.p.db.one("SELECT status, tries, landed_sha FROM push_queue") == {
        "status": "approved", "tries": 1, "landed_sha": None}
    assert s.p.db.task(s.review)["status"] == "pushing"
    assert s.p.db.one("SELECT outcome FROM push_batches")["outcome"] == "died"
    assert [e["status"] for e in _pq_events(s.p, mark) if e["kind"] == "push_batch_died"] == ["handled"]
    assert len(pushq.due(s.p, time.time() + 10 ** 6)[0]) == 1
    _pq_plan(s, outcome="pushed", sha="ab" * 20)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.task(s.review)["status"] == "done"
    assert s.p.db.one("SELECT COUNT(*) n FROM push_queue")["n"] == 1


def test_a_batch_that_died_mid_push_waits_for_an_unreadable_branch_then_tells_the_coordinator_once(env, monkeypatch):
    """Whether the dead batch's push landed cannot be told while the remote cannot be read: the batch
    waits (its approvals neither pushed again nor called landed). Past RECOVER_S it counts as died,
    its approvals go back, and the coordinator gets one event, since an after_push may be missing."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_pushing(s, s.head)
    _pq_batch(s)
    pushq._recover_asked.clear()
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None
    assert pushq.due(s.p, time.time() + 10 ** 6)[0] == []
    later = time.time() + pushq.RECOVER_S + 1
    pushq.finalize(s.p, now=later)
    pushq.finalize(s.p, now=later + 600)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_batch_died" and "could not be read" in ev["text"] and "after_push" in ev["text"]


def test_a_batch_that_died_mid_push_before_a_long_outage_is_still_recovered_once_the_remote_answers(env, monkeypatch):
    """The host was down longer than RECOVER_S after the batch began `git push`, and the network is not
    up on the first look: the window counts from that look, not from the push, so a later look that
    finds the head on the branch settles it as pushed, with no coordinator event and one after_push."""
    from ttp import pushq
    monkeypatch.setattr(pushq, "RECOVER_GAP_S", 0)
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    sha = _git_out(s.origin, "rev-parse", "proj")
    url = _git_out(s.p.root, "remote", "get-url", "origin")
    _pq_pushing(s, sha, at=time.time() - pushq.RECOVER_S - 3600)
    bid = _pq_batch(s)
    _git_out(s.p.root, "update-ref", "-d", "refs/remotes/origin/proj")   # only a fetch can tell
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_tend(s)
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None, "an old push is not died at once"
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    _git_out(s.p.root, "remote", "set-url", "origin", url)   # the network is back
    _pq_tend(s)
    assert s.p.db.one("SELECT outcome, pushed_sha FROM push_batches") == {"outcome": "pushed", "pushed_sha": sha}
    assert s.p.db.task(s.review)["status"] == "done"
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push, after_tries FROM push_batches") == {"after_push": "ok", "after_tries": 1}
    assert (s.p.state / "pushes" / f"{bid}.starts").read_text().split() == ["push", "after_push"]
    evs = _pq_events(s.p, mark)
    assert not [e for e in evs if e["status"] == "queued"], "recovered: the coordinator is not woken"
    assert not [e for e in evs if e["kind"] == "push_batch_died"]


def test_the_mid_push_recovery_window_counts_from_the_first_look_and_survives_a_restart(env, monkeypatch):
    """The first look is kept in the marker (pushing.first_checked): a restart between looks (the
    daemon's memory gone) neither restarts the window nor ends it early, and past RECOVER_S from that
    look the batch still dies `unverified`, with one event for the coordinator."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _git_out(s.p.root, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _pq_pushing(s, s.head, at=time.time() - pushq.RECOVER_S - 3600)
    bid = _pq_batch(s)
    pushq._recover_asked.clear()
    _pq_tend(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    first = json.loads(marker.read_text())["pushing"]["first_checked"]
    assert time.time() - 60 < first <= time.time()
    pushq._recover_asked.clear()                    # a restart: nothing kept in memory
    pushq.finalize(s.p, now=first + pushq.RECOVER_S - 60)
    assert json.loads(marker.read_text())["pushing"]["first_checked"] == first
    assert s.p.db.one("SELECT finalized FROM push_batches")["finalized"] is None
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "batched", "tries": 0}
    pushq._recover_asked.clear()
    pushq.finalize(s.p, now=first + pushq.RECOVER_S + 1)
    assert s.p.db.one("SELECT outcome FROM push_batches")["outcome"] == "died"
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 1}
    [ev] = [e for e in _pq_events(s.p, mark) if e["status"] == "queued"]
    assert ev["kind"] == "push_batch_died" and "could not be read" in ev["text"]


def test_a_real_batch_records_what_it_pushes_before_pushing_and_recovers_after_a_reboot(env, monkeypatch):
    """The real batch writes `pushing` (its head and each entry's result if it lands) before `git
    push`, matching what it writes once pushed. Cut off right after the push (the marker as a reboot
    leaves it, with the run lock and the target lock files still labelled), the queue settles it as
    pushed and runs the after_push once; the stale lock files hold nothing up for the next batch."""
    from ttp import pushq
    from ttp.project import write_json
    s = _pq_real(env, monkeypatch)
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / "pushes" / f"{bid}.json"
    m = json.loads(marker.read_text())
    sha = _git_out(s.origin, "rev-parse", "proj")
    assert m["outcome"] == "pushed" and m["pushing"]["sha"] == sha == m["pushed_sha"], m
    assert m["pushing"]["results"] == m["results"]
    # As a reboot leaves it: no outcome, phase push, the run lock and target lock files labelled but free.
    run_lock = s.p.state / "locks" / f"push:run-{bid}.0.lock"
    for path in (run_lock, s.p.state / "locks" / "push:origin%2Fproj.0.lock"):
        path.write_text(json.dumps({"holder": f"push batch {bid}", "since": time.time()}))
    gone = ("outcome", "results", "pushed_sha", "version", "tip", "checks", "rounds", "message", "conflicts",
            "reach", "after_push", "ended")
    write_json(marker, {**{k: v for k, v in m.items() if k not in gone},
                        "status": "running", "phase": "push", "lock": str(run_lock)})
    deploys = env["tmp"] / "deploys.log"
    s.p.set_config("delivery.after_push", [f"git rev-parse HEAD >> {deploys}"])
    _pq_tend(s)
    t = s.p.db.task(s.review)
    assert t["status"] == "done" and json.loads(t["result"])["pushed"][0]["sha"] == sha
    pushq._children[bid].wait(timeout=60)
    _pq_tend(s)
    assert deploys.read_text().split() == [sha]
    assert s.p.db.one("SELECT outcome, after_push FROM push_batches") == {"outcome": "pushed", "after_push": "ok"}
    assert not run_lock.exists()
    # The next approval goes out past the stale target lock file; the first is not pushed again.
    _commit(s.path, "more.txt", "more\n")
    rid, _ = _pq_review(s)
    with s.p.db.tx():
        s.p.db.x("UPDATE push_queue SET head=? WHERE task=?", (_git_out(s.path, "rev-parse", "HEAD"), rid))
    bid2 = _pq_batch(s)
    assert pushq._children[bid2].returncode == 0, (s.p.state / "pushes" / f"{bid2}.log").read_text()
    _pq_tend(s)
    assert s.p.db.task(rid)["status"] == "done"
    assert _git_out(s.origin, "log", "--format=%s", f"{sha}..proj").count("feature") == 0


@pytest.mark.parametrize("delivery, on", [
    ({"push_queue": True, "push_branch": "origin/proj"}, True),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": "yes"}, True),
    ({"push_queue": "yes", "push_branch": "origin/proj"}, False),
    ({"push_queue": 1, "push_branch": "origin/proj"}, False),
    ({"push_queue": True, "push_branch": " "}, False),
    ({"push_queue": True}, False),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": False}, False),
    ({"push_queue": True, "push_branch": "origin/proj", "push_allowed": "no"}, False),
    ({"push_queue": False, "push_branch": "origin/proj"}, False),
])
def test_the_daemon_and_the_review_prompt_agree_on_whether_the_push_queue_is_on(env, delivery, on):
    from ttp import pushq
    from ttp.project import push_queue_on
    p = make(env)
    cfg = {"delivery": delivery}
    assert push_queue_on(cfg) is on and pushq.enabled(p, cfg) is on
    for k, v in delivery.items():
        p.set_config(f"delivery.{k}", v)
    assert pushq.enabled(p) is on
    review = p.db.add_task("review it", "s", kind="review", tier="light", origin="user")
    from ttp.prompts import worker_task
    policy = next(x for x in worker_task(p, p.db.task(review), str(p.root), None).splitlines()
                  if x.startswith("delivery policy:"))
    assert ("push queue=True" in policy) is on
    if not on:   # an approval made anyway is not queued
        check = pushq.check_approval(p, p.db.task(review), [{"branch": "b", "head": "ab" * 20}])
        assert "entries" not in check, check


def test_an_after_push_failed_alert_clears_once_after_push_is_unset_not_when_the_queue_is_off(env, monkeypatch):
    from ttp import alerts, pushq
    s = _pq(env, monkeypatch, after_push="./deploy.sh")
    _pq_plan(s, outcome="pushed", sha="ab" * 20, after="failed")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT after_push FROM push_batches")["after_push"] == "failed"
    raised = s.p.db.one("SELECT raised FROM alerts WHERE key='after_push_failed' AND cleared IS NULL")["raised"]
    assert alerts.sweep(s.p.db) == [] and alerts.holds(s.p.db, "after_push_failed", raised, time.time())
    s.p.set_config("delivery.after_push", None)
    _pq_tend(s)
    assert s.p.db.kv(pushq.AFTER_PUSH_OFF) is True
    assert [ep["key"] for ep in alerts.sweep(s.p.db)] == ["after_push_failed"], "after_push unset clears it"
    s.p.set_config("delivery.after_push", "./deploy.sh")
    _pq_tend(s)
    assert s.p.db.kv(pushq.AFTER_PUSH_OFF) is False
    assert alerts.holds(s.p.db, "after_push_failed", raised, time.time()), "set again, the failure still stands"
    s.p.set_config("delivery.push_queue", False)
    _pq_tend(s)
    assert alerts.holds(s.p.db, "after_push_failed", raised, time.time()), \
        "with the queue off ordinary pushes still run after_push: the failure still stands"
    s.p.db.set_kv(pushq.AFTER_PUSH_OK, time.time())   # an ordinary push's after_push succeeded
    assert not alerts.holds(s.p.db, "after_push_failed", raised, time.time())


def test_approvals_ask_the_remote_for_its_default_branch_once_per_hour_not_once_each(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    pushq._default_branches.clear()
    asked, real = [], pushq._git

    def git(p, *args, **kw):
        if args[0] == "ls-remote":
            asked.append(args)
        return real(p, *args, **kw)
    monkeypatch.setattr(pushq, "_git", git)
    entries = [{"branch": s.branch, "head": s.head}]
    for _ in range(5):
        assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries"), "approved"
    assert len(asked) == 1, "five approvals in one tick, one ls-remote"
    # The remote's default branch becomes the target: refused once the cached answer expires.
    subprocess.run(["git", "-C", str(s.origin), "symbolic-ref", "HEAD", "refs/heads/proj"], check=True)
    now = time.time()
    assert pushq._default_branch(s.p, "origin", now + 60) != "proj" and len(asked) == 1
    assert pushq._default_branch(s.p, "origin", now + pushq.DEFAULT_BRANCH_TTL_S + 1) == "proj" and len(asked) == 2
    assert "the remote's default branch" in pushq.check_approval(s.p, s.p.db.task(s.review), entries)["invalid"]
    # An unreachable remote does not refuse, and is asked again only after a short pause.
    s.p.set_config("delivery.push_branch", "gone/proj")
    for _ in range(3):
        assert pushq._refusal(s.p, "gone", "proj") == ""
    assert len(asked) == 3
    assert pushq._default_branch(s.p, "gone", time.time() + pushq.DEFAULT_BRANCH_RETRY_S + 1) is None
    assert len(asked) == 4


def test_cancelling_or_requeueing_a_pushing_review_withdraws_its_approval(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_hand_off(env, s)
    s.p.db.update_task(s.review, status="cancelled")
    mark = _pq_mark(s.p)
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "cancelled"
    assert [(e["kind"], e["status"]) for e in _pq_events(s.p, mark)] == [("push_cancelled", "handled")]
    assert pushq.schedule(s.p) is None
    # A row already in a batch is not withdrawn: the batch reports it.
    rid, row = _pq_review(s)
    release = env["tmp"] / "go"
    _pq_plan(s, release=str(release), outcome="pushed", sha="ab" * 20)
    bid = _pq_batch(s, wait=False)
    s.p.db.update_task(rid, status="queued")
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue WHERE id=?", (row,))["status"] == "batched"
    release.write_text("")
    pushq._children[bid].wait(timeout=30)
    _pq_tend(s)
    assert s.p.db.task(rid)["status"] == "queued"
    [ev] = [e for e in _pq_events(s.p, mark) if e["kind"] == "push_reported"]
    assert ev["status"] == "queued" and "pushed" in ev["text"], "work that landed for a requeued review is news"


def test_task_resources_accept_push_lock_names_in_their_canonical_form(env):
    p = make(env)
    from ttp import coordinator as coord, locks, push
    canon = "push:origin%2Fteam%2Ffeature"   # the scheduler's lock file is the one ttp push holds
    assert locks.slot_paths(p.state / "locks", canon, 1) == push.lock_paths(p, "origin", "team/feature")
    problems = coord.apply(p, [{"type": "task_add", "title": "rev", "spec": "x", "kind": "review",
                                "resources": ["push:origin/team/feature", "push:origin%2Fteam%2Ffeature",
                                              "push:"]}])
    assert len(problems) == 1 and "'push:' dropped" in problems[0]
    labels = json.loads(p.db.one("SELECT labels FROM tasks WHERE title='rev'")["labels"])
    assert set(labels) == {"resource:" + canon}


def test_the_push_queues_batch_process_runs_niced(env, monkeypatch):
    from ttp import pushq
    p = make(env)
    stub = env["tmp"] / "nice_stub.py"
    stub.write_text("import os, sys\nsys.stdin.read()\nprint('nice', os.nice(0))\n")
    monkeypatch.setattr(pushq, "batch_argv", lambda marker: [sys.executable, str(stub)])
    marker = env["tmp"] / "batch.json"
    from ttp import checkdirs
    marker.write_text(json.dumps({"check_run_dir": str(checkdirs.new(p.state))}))
    for level in (4, 0):
        p.set_config("runner.nice", level)
        with open(env["tmp"] / "batch.lock", "w") as lock:
            child = pushq._spawn(p, f"b{level}", marker, lock)
        child.stdin.close()
        assert child.wait(timeout=30) == 0
        assert marker.with_suffix(".log").read_text().split()[-1] == str(min(os.nice(0) + level, 19))


def test_auto_upgrade_waits_for_a_push_then_runs_once_and_notifies_once(env, monkeypatch):
    from ttp import locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    assert p.config()["upgrade"]["auto"] is True, "on by default"
    held = locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push")
    d.check_release()
    assert not launches and p.db.kv("release"), "never during a push"
    assert d._release_due - time.time() < 600, "a held upgrade is looked at again soon, not in an hour"
    held.close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"
    notes = p.db.q("SELECT severity, text FROM messages WHERE text LIKE 'tt-project harness upgraded%'")
    assert len(notes) == 1 and notes[0]["severity"] == "low" and "(aaaa1111) to" in notes[0]["text"]
    for _ in range(2):
        d._release_due = 0
        d.check_release()
    assert launches == [p.base] and not p.db.kv("release")


def test_an_auto_upgrade_that_finds_a_push_at_its_swap_leaves_the_harness_and_retries(env, monkeypatch):
    from ttp import cli, locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    before = _git_out(p.harness, "rev-parse", "HEAD")
    held = []
    merge_upstream = cli._merge_upstream

    def push_starts_meanwhile(*a, **k):   # a push takes its lock while the upgrade merges
        out = merge_upstream(*a, **k)
        if not held:
            held.append(locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push"))
        return out
    monkeypatch.setattr(cli, "_merge_upstream", push_starts_meanwhile)
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "aaaa1111"
    assert _git_out(p.harness, "diff", "--stat", before, "HEAD", "--", "runtime") == "", "the live harness moved"
    assert p.db.kv("upgrade_auto")["outcome"] == "held" and p.db.kv("release")
    assert d._release_due - time.time() < 600, "retried soon, not in an hour"
    d._release_due = 0
    d.check_release()
    assert launches == [p.base], "never while the push is still in flight"
    held[0].close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base, p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"


def test_a_pushed_batch_resolves_the_upstream_notes_its_hand_offs_name_with_its_own_version(env, monkeypatch):
    """A code task's hand-off names the upstream notes it fixes (`resolves`); once the push queue's
    batch lands it, each is recorded as fixed in the version that batch pushed. Fingerprints of the
    task it continues count too; malformed ones are skipped; finalizing again records nothing more."""
    from ttp import pushq, upstream
    s = _pq(env, monkeypatch)
    notes = [{"title": f"upstream: n{i}", "spec": "s"} for i in range(3)]
    upstream.append("other", 2, notes)
    fps = [upstream.fingerprint(n["title"], "s") for n in notes]
    first = s.p.db.add_task("first try", "s", kind="code", origin="coordinator")
    s.p.db.update_task(first, status="failed", result=json.dumps({"status": "failed", "resolves": [fps[2]]}))
    s.p.db.update_task(s.code, result=json.dumps({"status": "done", "summary": "fixed",
                                                  "resolves": [fps[0].upper(), "not-a-fp", fps[1]]}),
                       labels=json.dumps([f"continues:{first}"]))
    _pq_plan(s, outcome="pushed", sha="ab" * 20, tip="cd" * 20, version="0.3.1")
    _pq_hand_off(env, s)
    assert upstream.state(fps[0]) == "open", "resolved before the push landed"
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status FROM push_queue")["status"] == "pushed"
    for fp in fps:
        r = upstream.resolution(fp)
        assert r and r["fixed_in"] == "0.3.1" and r["by"] == "demo", fp
    assert f"#{s.code}" in upstream.resolution(fps[0])["note"]
    assert f"#{first}" in upstream.resolution(fps[2])["note"]
    [ev] = s.p.db.q("SELECT status, severity, text FROM events WHERE kind='upstream_resolved'")
    assert ev["status"] == "handled" and "0.3.1" in ev["text"] and all(fp in ev["text"] for fp in fps)
    n = len(upstream._records())
    pushq._resolve_upstream(s.p, [s.review], "0.3.1")
    _pq_tend(s)
    assert len(upstream._records()) == n, "the same resolution was recorded twice"
    pushq._resolve_upstream(s.p, [s.review], None)
    assert len(upstream._records()) == n, "a batch without a version resolved notes"


def test_ttp_landed_finds_a_rebased_commit_by_patch_id_or_author_date_and_subject(env, monkeypatch, capsys):
    from ttp import cli, landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    raw = f"git merge-base --is-ancestor {head} origin/proj"
    assert subprocess.run(raw, shell=True, cwd=repo).returncode == 1, "the raw probe never passes after a rebase"
    assert landed.probe_command(raw) == f"ttp landed {head} --onto origin/proj"
    assert landed.probe_command("landed:#42") == "ttp landed --task 42" == landed.probe_command(" landed: 42 ")
    assert landed.probe_command("test -f x") == "test -f x"

    def ttp_landed(*args):
        with pytest.raises(SystemExit) as e:
            cli.main(["landed", *args])
        return e.value.code
    assert ttp_landed(head) == 0 and ttp_landed(head[:10], "--onto", "origin/proj") == 0
    assert ttp_landed(f"#{tid}") == 0 and ttp_landed("--task", str(tid)) == 0, "the task's branch, by patch"
    # A settled conflict changes the patch; the same author, date and subject still match.
    _git_out(other, "commit", "-q", "--amend", "--no-edit", "--allow-empty", "--only", "--", ".")
    (other / "feature.txt").write_text("feature, settled\n")
    _git_out(other, "commit", "-q", "--amend", "--no-edit", "-a")
    _git_out(other, "push", "-q", "-f", "origin", "HEAD:proj")
    assert ttp_landed(head) == 0
    # Not landed: a new commit, a commit nowhere, a task with nothing.
    _commit(path, "more.txt", "more\n")
    assert ttp_landed(_git_out(path, "rev-parse", "HEAD")) == 1 and ttp_landed(f"#{tid}") == 1
    assert ttp_landed("0" * 40) == 2 and ttp_landed("--task", "9999") == 2, "no such task: it never lands"
    assert "not on origin/proj yet" in capsys.readouterr().out


def test_landed_task_prefers_the_push_queues_recorded_commit_and_waits_until_it_is_on_the_branch(env, monkeypatch):
    """A review's approval the queue landed counts for the code task it reviews: its landed_sha."""
    from ttp import landed
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    (path / "feature.txt").write_text("feature, redone\n")   # its branch no longer matches what landed
    _git_out(path, "commit", "-q", "--amend", "-am", "redone after the landing")
    assert landed.check(p, repo, tid=tid)[0] == 1
    review = p.db.add_task(f"Review #{tid}", "r", kind="review", origin="coordinator")
    rebased = _git_out(other, "rev-parse", "HEAD")
    now = time.time()
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
           "VALUES(?, 'other-branch', ?, 'origin/proj', 'approved', ?, ?, NULL, NULL)", (review, head, now, now))
    assert landed.check(p, repo, tid=tid)[0] == 1, "an approval still in the queue has not landed"
    p.db.x("UPDATE push_queue SET status='pushed', pushed_sha=?, landed_sha=?", (rebased, rebased))
    assert landed.task_shas(p, tid) == ([rebased], "its push queue landing")
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its push queue landing) is on origin/proj")


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "done"])
def test_landed_task_never_passes_for_a_task_that_has_not_landed_anything(env, monkeypatch, status):
    """The daemon makes a task's branch at the push branch's tip when it starts, so the branch head is
    already on the branch: that must not read as landed while the task runs, after it failed or was
    cancelled, or when it finished without a commit. A deferral on it stays deferred."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path, branch = _code_task(p, "feature", status=status)
    assert landed.on_branch(repo, _git_out(path, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "origin/proj"))
    rc, words = landed.check(p, repo, tid=tid)
    assert rc == 1 and landed.task_shas(p, tid)[0] == [], words
    assert ("no commits on its branch" if status == "done" else f"has not finished ({status})") in words
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    after = _added(p, "after it")
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    _settle_deferred(d, after)
    assert not _ready(p, after) and not _events(p, after, "deferral_probe_broken")


def test_landed_task_counts_a_done_tasks_own_commits_once_they_are_on_the_branch(env, monkeypatch):
    """The branch fallback still works for a done task with a commit, landed by fast-forward."""
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    _commit(path, "feature.txt", "feature\n")
    assert landed.check(p, repo, tid=tid)[0] == 1, "still running"
    p.db.update_task(tid, status="done")
    assert landed.check(p, repo, tid=tid)[0] == 1, "done, not pushed yet"
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its branch {branch}) is on origin/proj")


@pytest.mark.parametrize("chain", ["rebase", "review_fix"])
def test_landed_task_follows_a_successor_that_landed_its_work_under_other_patches(env, monkeypatch, chain):
    """A done task's change landed through a successor on a branch of its own with other patches
    (a rebase task that settled a conflict, or a review fix and its re-review): landed:#<orig> passes
    once the successor's push queue entry lands, and a task deferred on it starts."""
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    tid, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    review = p.db.add_task(f"Review #{tid}: feature", "r", kind="review", status="done")
    unrelated, upath, ubranch = _code_task(p, "unrelated")
    if chain == "rebase":
        succ, spath, sbranch = _code_task(p, f"rebase #{tid} onto the tip")
        p.db.update_task(succ, labels=json.dumps([f"rebases:{branch}", f"continues:{review}"]))
        lander = p.db.add_task(f"Review #{succ}: rebased #{tid} on the tip", "r", kind="review", status="done")
    else:
        succ, spath, sbranch = _code_task(p, f"Fix review #{review}")
        p.db.update_task(succ, labels=json.dumps([f"continues:{tid}", f"review_fix:{review}"]))
        lander = p.db.add_task(f"Re-review #{review}: feature", "r", kind="review", status="done",
                               labels=[f"auto_review:{succ}", f"continues:{review}"])
    (spath / "feature.txt").write_text("feature, settled\n")   # not the original's patch
    (spath / "fix.txt").write_text("fix\n")
    _git_out(spath, "add", "-A")
    _git_out(spath, "commit", "-qm", "the change, settled on the tip")
    assert landed.check(p, repo, tid=tid)[0] == 1, "the original's own branch is not on the branch"
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    after = _added(p, "after it")
    d = dmod.Daemon(p.base)
    _settle_deferred(d, after)
    assert not _ready(p, after)

    def land(task, br, path):
        _git_out(path, "fetch", "-q", "origin")
        _git_out(path, "rebase", "-q", "origin/proj")
        _git_out(path, "push", "-q", "origin", "HEAD:proj")
        sha, now = _git_out(path, "rev-parse", "HEAD"), time.time()
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated, pushed_sha, landed_sha) "
               "VALUES(?, ?, ?, 'origin/proj', 'pushed', ?, ?, ?, ?)", (task, br, sha, now, now, sha, sha))
        return sha
    _commit(upath, "unrelated.txt", "unrelated\n")
    land(unrelated, ubranch, upath)
    assert landed.check(p, repo, tid=tid)[0] == 1, "another task's landing is not this one's"
    land(lander, sbranch, spath)
    assert landed.check(p, repo, tid=tid) == (0, f"#{tid} (its push queue landing through #{lander}) is on origin/proj")
    assert landed.check(p, repo, tid=unrelated)[0] == 0 and set(landed.carriers(p.db, unrelated)) == {unrelated}
    _settle_deferred(d, after, until=lambda: _ready(p, after))
    assert _ready(p, after) and not _events(p, after, "deferral_probe_broken")


@pytest.mark.parametrize("result_status", ["waiting", "blocked", "failed", "needs_review"])
def test_a_detached_push_records_a_landing_even_with_a_non_done_handoff(env, monkeypatch, result_status):
    from ttp import landed, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    sha = _git_out(other, "rev-parse", "HEAD")
    # Its branch moved after the push: the marker's rebased sha is the evidence, not its head.
    _commit(path, "later.txt", "later\n")
    p.db.update_task(tid, status="running")
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "head": head,
                                  "task": str(tid), "target": "origin/proj", "own": False}))
    run_dir = env["tmp"] / "push-run"
    run_dir.mkdir()
    result = {"status": result_status, "summary": "push finished; further work remains",
              "retry_when": "true", "waiting_for": "push", "retry_after_s": 1800}
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    d._finish_worker({"task": tid, "note": "{}"}, RunUsage(), "ok", run_dir)
    assert landed.check(p, repo, tid=tid)[0] == 0
    assert json.loads(p.db.task(tid)["result"])["status"] == result_status
    d.tend_pushes()
    d.tend_pushes()
    assert p.db.q("SELECT task, sha FROM task_landings") == [{"task": tid, "sha": sha}]
    # Evidence survives marker pruning and removal of the task's worktree/branch.
    marker.unlink()
    p.db.update_task(tid, branch=None)
    assert landed.check(p, repo, tid=tid)[0] == 0


def test_daemon_restart_recovers_a_detached_push_landing_and_releases_its_dependents(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp import landed, push
    p, repo, other, tid, path, branch, head = _landed_setup(env, monkeypatch)
    p.db.update_task(tid, status="queued", branch=None,
                     result=json.dumps({"status": "waiting", "retry_when": "true"}))
    assert coord.apply(p, [{"type": "task_add", "title": "after detached push", "spec": "s",
                            "start_when": f"landed:#{tid}"}]) == []
    dependent = _added(p, "after detached push")
    dmod.Daemon(p.base)   # stopped before it could record the landing
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    sha = _git_out(other, "rev-parse", "HEAD")
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "task": str(tid),
                                  "target": "origin/proj", "own": False}))
    p.db.x("DROP TABLE task_landings")   # upgrading a daemon whose DB predates landing receipts
    d = dmod.Daemon(p.base)
    d.tend_pushes()
    assert landed.check(p, repo, tid=tid)[0] == 0
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    _settle_deferred(d, dependent, until=lambda: _ready(p, dependent))
    assert _ready(p, dependent)
    dmod.Daemon(p.base).tend_pushes()
    assert p.db.q("SELECT task, sha FROM task_landings") == [{"task": tid, "sha": sha}]


@pytest.mark.parametrize("bad", [
    {"status": "queued"}, {"status": "running"}, {"status": "failed"},
    {"own": True}, {"kind": "batch"}, {"sha": None}, {"sha": "bad"},
    {"task": None}, {"task": "not-a-task"}, {"task": "99999"}, {"target": None},
])
def test_detached_push_landing_ignores_publications_and_incomplete_or_failed_markers(env, monkeypatch, bad):
    from ttp import landed, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    marker = p.state / push.DETACHED / f"t{tid}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": _git_out(repo, "rev-parse", "HEAD"),
                                  "task": str(tid), "target": "origin/proj", "own": False, **bad}))
    landed.reconcile_pushes(p, tid)
    assert not p.db.q("SELECT * FROM task_landings")
    assert landed.check(p, repo, tid=tid)[0] == 1


def test_a_recorded_detached_push_still_checks_the_push_branch_and_covers_its_reviewed_task(env, monkeypatch):
    from ttp import landed, push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    tid, path, branch = _code_task(p, "feature", status="running")
    _commit(path, "feature.txt", "feature\n")
    sha = _git_out(path, "rev-parse", "HEAD")
    review = p.db.add_task(f"Review #{tid}: feature", "review", kind="review", status="running")
    marker = p.state / push.DETACHED / f"t{review}-push.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"status": "pushed", "sha": sha, "task": str(review),
                                  "target": "origin/proj", "own": False}))
    landed.reconcile_pushes(p, review)
    assert landed.task_shas(p, tid) == ([sha], "its detached push landing")
    assert landed.check(p, repo, tid=tid)[0] == 1, "a receipt never replaces the branch check"
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    assert landed.check(p, repo, tid=tid)[0] == landed.check(p, repo, tid=review)[0] == 0


@pytest.mark.parametrize("cmd", [
    'h=$(git rev-parse HEAD); test "$h" = 0000 || pytest -q',
    'h="$(git rev-parse HEAD)" && test -n "$h"',
    "h=`git rev-parse HEAD`; true",
    "h=$(cd sub && git rev-parse $(echo HEAD)); pytest -q",
    "set -e; pytest -q",
    "x=1",
    'A=1 B="x y" pytest -q',
    "n=$((1+2)); pytest -q",
    'n="$(( 3 * 4 ))" && test "$n" = 12',
    "h=$(git rev-parse HEAD) pytest -q",
])
def test_check_problem_accepts_a_bare_assignment_holding_a_command_substitution(env, cmd):
    # A scoped check (`h=$(git rev-parse HEAD); ...`) is a command; shlex used to split the
    # substitution into words and reject `rev-parse` as the first one.
    from ttp.push import check_problem
    assert check_problem(cmd) is None


@pytest.mark.parametrize("cmd,why", [
    ("Run all the tests", "'Run' is not a program on PATH"),
    ("h=$(Run the tests); true", "'Run' is not a program on PATH"),
    ("Run=all the tests", "'the' is not a program on PATH"),
    ("h=$(git rev-parse HEAD", "does not parse as a shell command"),
    ("h=$(Run the tests) pytest -q", "'Run' is not a program on PATH"),
    ('h="$(Run the tests)" pytest -q', "'Run' is not a program on PATH"),
])
def test_check_problem_still_rejects_prose_and_unclosed_substitutions(env, cmd, why):
    from ttp.push import check_problem
    assert why in (check_problem(cmd) or "")


def test_push_check_path_args_and_unmatched(env, tmp_path):
    from ttp import push
    assert push.path_args("python3 -m pytest -q tests/test_a.py::test_x tests/b_*.py") == \
        ["tests/test_a.py", "tests/b_*.py"]
    assert push.path_args("make check") == []
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    checks = ["pytest tests/test_a.py tests/test_gone.py", "ruff check tests/*.py", "pytest tests/"]
    ref, missing = push.unmatched_paths(repo, "origin", "work", checks)
    assert ref == "work"
    assert missing == ["'pytest tests/test_a.py tests/test_gone.py': 'tests/test_gone.py'"]
    assert push.unmatched_paths(repo, "origin", "nope", checks) == ("", [])
    # pytest parametrised ids are not globs
    assert push.path_args("pytest tests/test_a.py[param-1] tests/test_a.py::test_x[a-b]") == \
        ["tests/test_a.py", "tests/test_a.py"]
    # after `cd <dir>` paths are relative to <dir>; the dir itself is checked too
    assert push.path_args("cd plugins/x && pytest tests/ ../../tests/test_a.py") == \
        ["plugins/x", "plugins/x/tests", "tests/test_a.py"]
    assert push.path_args("cd /tmp && pytest tests/") == []
    assert push.path_args("cd $HOME && pytest tests/") == []
    # Redirect targets and option values are not check targets.
    assert push.path_args("pytest tests/a.py > out/log.txt 2> out/err 2>>out/x >out/y < in/z") == ["tests/a.py"]
    assert push.path_args("pytest --junitxml out/r.xml --rootdir=sub/dir -q tests/a.py") == ["tests/a.py"]
    assert push.path_args("pytest --junitxml=out/r.xml -c cfg/p.ini tests/b.py") == ["tests/b.py"]
    (repo / "plugins" / "x" / "tests").mkdir(parents=True)
    (repo / "plugins" / "x" / "tests" / "test_p.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "p")
    cd_checks = ["cd plugins/x && pytest tests/ tests/test_p.py[a]", "cd plugins/gone && pytest tests/"]
    assert push.unmatched_paths(repo, "origin", "work", cd_checks)[1] == [
        "'cd plugins/gone && pytest tests/': 'plugins/gone'"]
    # the remote-tracking ref (what `ttp push` rebases onto) wins over a stale local branch
    g("checkout", "-qb", "newer")
    (repo / "tests" / "test_gone.py").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y")
    g("update-ref", "refs/remotes/origin/work", "HEAD")
    assert push.unmatched_paths(repo, "origin", "work", checks) == ("origin/work", [])


def test_whitespace_check_skips_logs_and_no_diff_files(env, tmp_path, capsys):
    """The documented `git diff --check` form leaves out committed run logs (their captured lines
    keep trailing whitespace) and files .gitattributes marks -diff or binary, but still fails on
    source; doctor names a configured whitespace check that would fail on logs."""
    from ttp import push
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    g("commit", "-q", "--allow-empty", "-m", "base")
    g("branch", "base")
    check = push.whitespace_check("base")
    run = lambda c: subprocess.run(c, shell=True, cwd=repo, capture_output=True, text=True)
    (repo / "runs").mkdir()
    (repo / "runs" / "device.log").write_text("step 1 ok   \nstep 2\t\n")
    (repo / "out.txt").write_text("captured   \n")
    (repo / "blob.dat").write_text("raw   \n")
    (repo / ".gitattributes").write_text("out.txt -diff\nblob.dat binary\n")
    g("add", ".")
    g("commit", "-qm", "logs")
    assert run("git diff --check base HEAD").returncode != 0   # the bare form fails on the log
    r = run(check)
    assert r.returncode == 0, r.stdout   # (a) the log and (c) the -diff and binary files are skipped
    (repo / "src.py").write_text("x = 1   \n")
    g("add", ".")
    g("commit", "-qm", "src")
    r = run(check)
    assert r.returncode != 0 and "src.py" in r.stdout and ".log" not in r.stdout   # (b) source still fails
    plain = "git diff --check origin/work HEAD"
    assert push.unexcluded_log_checks([plain, push.whitespace_check("origin/work"), "pytest -q",
                                       "git -C . diff --check a b -- . ':!*.log'", "git diff a b"]) == [plain]
    p = make(env)
    p.set_config("delivery.push_checks", [plain, "pytest -q"])
    from ttp import cli
    cli.main(["doctor", p.name])
    out = capsys.readouterr().out
    assert f"delivery.push_checks: {plain!r} also checks committed *.log output" in out
    assert p.config()["delivery"]["push_checks"] == [plain, "pytest -q"]   # explicit checks stay as set


def test_check_objects_are_validated_kept_in_config_and_plain_strings_stay_strict(env):
    from ttp import push
    from ttp.project import config_problems
    checks = push.check_list(["true", COND, {"run": "true", "if_exists": "tests/*.sh"}])
    assert checks == ["true", f"sh {NEW_TEST}", "true"]
    assert [c.if_exists for c in checks] == ["", NEW_TEST, "tests/*.sh"]
    assert push.check_list(json.dumps([COND]))[0].if_exists == NEW_TEST, "config_set sends JSON text"
    assert push.checks_of(["true", COND]) == ["true", COND], "the opt-in form must survive config_set"
    for bad in ({"if_exists": "x"}, {"run": "true", "if_exists": "/abs"}, {"run": "true", "if_exists": "../up"},
                {"run": "true", "if_exists": ""}, {"run": "true", "paths": "x"}, {"run": "true", "if_exists": 3}):
        with pytest.raises(ValueError):
            push.checks_of([bad])
        assert config_problems({"delivery": {"push_checks": [bad]}}), bad
    # A malformed object never skips: it runs on every head, as a plain string does.
    assert [c.if_exists for c in push.check_list([{"run": "true", "if_exists": "../up"},
                                                   {"run": "true", "if_exists": "x", "extra": 1}])] == ["", ""]
    p = make(env)
    from ttp import cli
    cli.main(["config", p.name, "delivery.push_checks", json.dumps(["true", COND])])
    assert p.config()["delivery"]["push_checks"] == ["true", COND]


def test_if_exists_with_a_trailing_slash_or_leading_dot_slash_still_matches(env, tmp_path):
    from ttp import push
    import subprocess
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "a.sh").write_text("true\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    checks = push.check_list([{"run": "true", "if_exists": "tests/"}, {"run": "true", "if_exists": "./tests/a.sh"}])
    assert [c.if_exists for c in checks] == ["tests", "tests/a.sh"]
    assert [push.skip_reason(repo, "HEAD", c) for c in checks] == [None, None]


def test_check_commands_run_bare_pytest_with_the_repositorys_venv(tmp_path):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    q = shlex.quote(str(py))
    assert push.venv_cmd("pytest -q", repo) == f"{q} -m pytest -q"
    assert push.venv_cmd("python3 -m pytest -q tests/a.py", repo) == f"{q} -m pytest -q tests/a.py"
    assert push.venv_cmd("FOO=1 py.test -x && ruff check", repo) == f"FOO=1 {q} -m pytest -x && ruff check"
    assert push.venv_cmd("cd sub && pytest", repo) == f"cd sub && {q} -m pytest"
    for other in ("make test", "echo pytest", "pytest-benchmark run", "python3 tools/x.py"):
        assert push.venv_cmd(other, repo) == other
    # A linked worktree has no copy of the ignored venv: the main checkout's is used.
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "x", str(wt)], check=True)
    assert push.venv_cmd("pytest -q", wt) == f"{q} -m pytest -q"
    # No venv, or one without pytest: the command stays as written.
    bare, _ = _venv_repo(tmp_path, "nopytest", pytest_too=False)
    assert push.venv_cmd("pytest -q", bare) == "pytest -q"
    plain = tmp_path / "plain"
    subprocess.run(["git", "init", "-q", str(plain)], check=True)
    assert push.venv_cmd("pytest -q", plain) == "pytest -q"
    assert push.check_argv("pytest -q")[-1] == "pytest -q"   # without a worktree: as written


def _nested_worktree(tmp_path):
    """A project root with a pytest config, a conftest.py and a package `pkg`, and a git worktree nested
    below it whose `pkg` differs (it alone has `pkg.only_here`), with a test that imports it."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "conftest.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("WHERE = 'root'\n")
    wt = root / "tt-project" / "worktrees" / "t1"
    (wt / "pkg").mkdir(parents=True)
    (wt / "tests").mkdir()
    (wt / "pkg" / "__init__.py").write_text("WHERE = 'worktree'\n")
    (wt / "pkg" / "only_here.py").write_text("")
    (wt / "tests" / "test_pkg.py").write_text(
        "import pkg\nfrom pkg import only_here\n\ndef test_pkg():\n    assert pkg.WHERE == 'worktree'\n")
    git = ["git", "-C", str(wt), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    shim = tmp_path / "shim"   # the python3 a check command names: this interpreter, which has pytest
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    return root, wt, shim


def test_pytest_in_a_nested_worktree_imports_the_worktrees_package(env, tmp_path, monkeypatch):
    from ttp import cli, push
    root, wt, shim = _nested_worktree(tmp_path)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cmd = "python3 -m pytest -q -p no:cacheprovider tests/test_pkg.py"
    # As written, pytest takes the project root as rootdir, loads its conftest.py and imports its pkg.
    plain = subprocess.run(["bash", "-c", cmd], cwd=wt, capture_output=True, text=True)
    assert plain.returncode != 0 and "only_here" in plain.stdout, plain.stdout
    assert push.nested_pytest(wt) and not push.nested_pytest(tmp_path / "shim")
    q = shlex.quote(str(wt))
    assert push.venv_cmd(cmd, wt) == (f" PYTHONPATH={q}${{PYTHONPATH:+:$PYTHONPATH}} python3 -m pytest "
                                      f"--rootdir={q} --confcutdir={q} -q -p no:cacheprovider tests/test_pkg.py")
    # A command's own --rootdir and PYTHONPATH stay its own.
    assert push.venv_cmd("cd x && PYTHONPATH=src pytest --rootdir=x", wt) == \
        f"cd x && PYTHONPATH={q}${{PYTHONPATH:+:$PYTHONPATH}} PYTHONPATH=src pytest --confcutdir={q} --rootdir=x"
    assert push.venv_cmd("make test", wt) == "make test"
    p = make(env)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(wt)
    cli.main(["checks", "--", cmd])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["commands"] == [cmd], (run / "checks.log").read_text()


def test_check_commands_run_wrapped_pytest_with_the_venv(tmp_path):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    q = shlex.quote(str(py))
    assert push.venv_cmd("timeout 600 pytest -q", repo) == f"timeout 600 {q} -m pytest -q"
    assert push.venv_cmd("timeout -k 5 --preserve-status 10m python3 -m pytest", repo) == \
        f"timeout -k 5 --preserve-status 10m {q} -m pytest"
    assert push.venv_cmd("env X=1 pytest -x", repo) == f"env X=1 {q} -m pytest -x"
    assert push.venv_cmd("make && env A=1 timeout 60 pytest", repo) == f"make && env A=1 timeout 60 {q} -m pytest"
    for other in ("timeout pytest", "envoy pytest", "timeout 60 make test"):
        assert push.venv_cmd(other, repo) == other


def _editable(venv: Path, kind: str, src: Path) -> None:
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    if kind == "direct_url":
        (site / "mypkg-0.1.dist-info").mkdir(exist_ok=True)
        (site / "mypkg-0.1.dist-info" / "direct_url.json").write_text(
            json.dumps({"url": src.as_uri(), "dir_info": {"editable": True}}))
    elif kind == "finder":
        (site / "__editable___mypkg_0_1_finder.py").write_text(f"MAPPING = {{'mypkg': '{src / 'mypkg'}'}}\n")
    else:
        (site / "__editable__.mypkg-0.1.pth").write_text(f"{src}\n")


@pytest.mark.parametrize("kind", ["direct_url", "finder", "pth"])
def test_a_worktree_skips_the_main_checkouts_venv_with_an_editable_install_of_it(tmp_path, kind):
    from ttp import push
    repo, py = _venv_repo(tmp_path)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "x", str(wt)], check=True)
    q = shlex.quote(str(py))
    # A non-editable install, or one from elsewhere, keeps the venv in use.
    _editable(repo / ".venv", kind, tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "mypkg").mkdir(parents=True)
    assert push.venv_cmd("pytest -q", wt) == f"{q} -m pytest -q"
    # Installed editable from the main checkout: the worktree's tests would import its code, so the
    # command runs as written and the checks output says why.
    _editable(repo / ".venv", kind, repo)
    cmd = push.venv_cmd("pytest -q", wt)
    assert cmd.endswith("\npytest -q") and str(py) not in cmd.splitlines()[-1]
    said = subprocess.run(["bash", "-c", cmd.replace("pytest -q", "true")], capture_output=True, text=True).stderr
    assert "installed editable from" in said and str(repo) in said and "outside this worktree" in said
    # In the main checkout itself the install is its own code: the venv is used.
    assert push.venv_cmd("pytest -q", repo) == f"{q} -m pytest -q"


def test_env_problem_tells_missing_dependencies_from_code_failures(tmp_path):
    from ttp import push
    repo, _ = _venv_repo(tmp_path)
    why = push.env_problem(CONFTEST_FAIL.format(mod="numpy"), repo)
    assert why and "numpy" in why and push.ENV_FIX in why
    collect = ("==== ERRORS ====\n___ ERROR collecting tests/test_a.py ___\nImportError while importing test "
               "module '/w/tests/test_a.py'.\nE   ModuleNotFoundError: No module named 'yaml.loader'\n")
    assert "yaml" in push.env_problem(collect, repo)
    assert push.ENV_FIX in push.env_problem("/usr/bin/python3: No module named pytest\n", repo)
    assert push.ENV_FIX in push.env_problem("bash: line 1: pytest: command not found\n", repo)
    # The repository's own module failing to import is the head's code, as is any other failure.
    assert push.env_problem(CONFTEST_FAIL.format(mod="mypkg.sub"), repo) is None
    assert push.env_problem("E   AssertionError: 1 != 2\n1 failed in 0.1s\n", repo) is None
    assert push.env_problem("No module named 'numpy'\n", repo) is None   # not a collection or import failure



def test_if_changed_is_validated_kept_in_config_and_malformed_scopes_never_skip(env):
    from ttp import push
    from ttp.project import config_problems
    checks = push.check_list([SCOPED, {"run": "true", "if_changed": "./src/"}])
    assert [c.if_changed for c in checks] == [("src/**", "fail.sh"), ("src",)]
    assert push.checks_of([SCOPED, {"run": "true", "if_changed": "src"}]) == [SCOPED, {"run": "true", "if_changed": "src"}]
    assert push.check_list([{**COND, "if_changed": "src"}])[0].config() == {**COND, "if_changed": "src"}
    for bad in ({"run": "true", "if_changed": ""}, {"run": "true", "if_changed": []}, {"run": "true", "if_changed": "/abs"},
                {"run": "true", "if_changed": ["src", "../up"]}, {"run": "true", "if_changed": 3},
                {"run": "true", "if_changed": [""]}):
        with pytest.raises(ValueError):
            push.checks_of([bad])
        assert config_problems({"delivery": {"push_checks": [bad]}}), bad
        assert push.check_list([bad])[0].if_changed == (), "a malformed scope must run the check everywhere"


def test_push_runs_an_if_changed_check_only_when_the_diff_touches_its_paths(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true", SCOPED])
    _commit(repo, "fail.sh", "exit 1\n")
    assert _ttp_push() == 4, "a diff touching the check's path must run it, and its failure stops the push"
    assert "skipped" not in capsys.readouterr().err
    _git_out(repo, "reset", "-q", "--hard", "origin/proj")
    _git_out(other, "pull", "-q")
    _commit(other, "fail.sh", "exit 1\n")   # on the branch already: not this change's diff
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "pull", "-q", "origin", "proj")
    _commit(repo, "notes.txt", "notes\n")
    assert _ttp_push() == 0, "a notes-only change was blocked by a check scoped to code"
    err = capsys.readouterr().err
    assert "skipped (not applicable:" in err and "changes nothing under src/**, fail.sh since" in err, err
    # The glob crosses subdirectories, and a move out of scope still counts as touching it.
    (repo / "src" / "a").mkdir(parents=True)
    _commit(repo, "src/a/x.py", "x\n")
    assert _ttp_push() == 4
    from ttp import push
    _git_out(repo, "reset", "-q", "--hard", "origin/proj")
    (repo / "src").mkdir()
    _commit(repo, "src/x.py", "x\n")
    base = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "mv", "src/x.py", "x.py")
    _git_out(repo, "commit", "-qm", "move")
    check = push.check_list([SCOPED])
    assert push.applicable(repo, "HEAD", check, print, base)[0] == check
    assert push.skip_reason(repo, "HEAD", check[0], base) is None   # after_push scopes the same way


def test_an_if_changed_check_fails_when_git_cannot_give_the_diff(env, monkeypatch, tmp_path):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, [SCOPED])
    _commit(repo, "notes.txt", "notes\n")
    said = []
    check = push.check_list([SCOPED])
    for base in ("0" * 40, "no-such-ref"):
        with pytest.raises(push.ScopeError) as e:
            push.applicable(repo, "HEAD", check, said.append, base)
        assert "cannot tell what HEAD changes since" in str(e.value) and e.value.check == "sh fail.sh"
    assert said == []
    # No push target to compare with: the check is not scoped, so it runs.
    assert push.applicable(repo, "HEAD", check, said.append, "") == (check, [])
    # Unrelated history (no merge base) is an error too, not "nothing changed".
    lone = tmp_path / "lone"
    subprocess.run(["git", "init", "-q", str(lone)], check=True)
    _commit(lone, "x.txt", "x\n")
    _git_out(repo, "fetch", "-q", str(lone), "HEAD:refs/heads/lone")
    with pytest.raises(push.ScopeError):
        push.applicable(repo, "HEAD", check, said.append, "lone")

@pytest.mark.parametrize("box", ["pidns", "cgroup"])
def test_sandboxed_checks_are_queued_and_finish_model_free(env, tmp_path, monkeypatch, capsys, box):
    from ttp import cli, detach, push
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    if not (push.pid_ns() if box == "pidns" else push.cgroup()):
        pytest.skip("no /proc")
    p = make(env)
    tid, repo = _checks_wait_task(p, tmp_path, {"status": "done", "summary": "checks passed"}, rc=None)
    run = pathlib.Path(p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))["dir"])
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    marker = "TTP_PIDNS" if box == "pidns" else "TTP_CGROUP"
    monkeypatch.setenv(marker, "another sandbox")
    monkeypatch.setenv("SOME_API_TOKEN", "keep-off-disk")
    monkeypatch.setenv("CHECK_VALUE", "preserved")
    cli.main(["checks", "--detach", "--fresh", "--", 'test "$CHECK_VALUE" = preserved'])
    assert "queued" in capsys.readouterr().out
    assert not (run / cli.CHECKS_PID).exists(), "the sandbox must launch no child"
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert "keep-off-disk" not in q.read_text() and marker not in json.loads(q.read_text())["env"]
    assert cli._checks_result(run) == 1 and not (run / cli.CHECKS_RC).exists()
    monkeypatch.delenv(marker)
    d = Daemon(p.base)
    d.boot_at = time.time() - 30   # queued work survives a daemon restart or host reboot
    d.wake_after_reboot()
    assert p.db.task(tid)["not_before"] and "woke" not in json.loads(p.db.task(tid)["result"])
    d.tend_pushes()
    info = cli._read_checks_pid(run)
    assert info["pid"] and info["started"] == proc_start(info["pid"])
    assert info["worktree"] == str(repo) and info["fresh"] is True
    assert _wait_precheck(run) == "0"
    assert not q.exists() and not detach.queued(run / cli.CHECKS_RC).exists()
    assert cli._checks_result(run) == 0
    _settle_probe(d, tid)
    assert p.db.task(tid)["status"] == "done"
    assert json.loads(p.db.task(tid)["result"])["run_status"] == "model-free"
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1


def test_queued_checks_keep_full(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--full", "--", "true"])
    assert "queued" in capsys.readouterr().out
    q = next((p.state / detach.QUEUE).glob("*.json"))
    assert json.loads(q.read_text())["checks"]["full"] is True
    monkeypatch.setattr(push, "_boxed", lambda: False)
    seen = _record_checks_argv(monkeypatch)
    detach.tend(p)
    assert seen and "--full" in seen[-1], seen
    assert cli._read_checks_pid(run)["full"] is True
    assert _wait_precheck(run) == "0"


def test_queued_checks_death_is_not_hidden_by_a_reused_pid(env, tmp_path, monkeypatch, capsys, reap_tmp_processes):
    from ttp import cli, detach, push
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    started = tmp_path / "started"
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "sh", "-c", f"touch {started}; sleep 60"])
    detach.tend(p)
    monkeypatch.setattr(push, "_boxed", lambda: False)   # the daemon's probe, on the host, judges pids
    info = cli._read_checks_pid(run)
    try:
        assert _wait_for(started.exists)
        os.killpg(info["pid"], signal.SIGKILL)
        assert _wait_for(lambda: _gone_pid(info["pid"]))
        assert not (run / cli.CHECKS_RC).exists()
        # Simulate the dead child's pid being reused by a live, unrelated process.
        info.update(pid=os.getpid(), started="previous process incarnation")
        assert proc_start(info["pid"]) != info["started"]
        (run / cli.CHECKS_PID).write_text(json.dumps(info))
        assert cli._checks_result(run) == 0, "a pid collision must not leave the task asleep"
        assert (run / cli.CHECKS_RC).read_text().strip() == "killed"
        os.kill(os.getpid(), 0)   # the unrelated process remains alive
    finally:
        if info["pid"] != os.getpid():
            try:
                os.killpg(info["pid"], signal.SIGKILL)
            except OSError:
                pass
        reap_tmp_processes(tmp_path)   # the check, a session of its own, outlives its killed supervisor


def test_push_priority_key_names_the_branch_host_wide(env, tmp_path):
    from ttp import push
    a, b = tmp_path / "a", tmp_path / "b"
    for r, url in ((a, "https://example.com/org/repo.git"), (b, "https://example.com/org/repo/")):
        subprocess.run(["git", "init", "-q", str(r)], check=True)
        subprocess.run(["git", "-C", str(r), "remote", "add", "origin", url], check=True)
    assert push.priority_key(a, "origin", "main") == push.priority_key(b, "origin", "main"), "two clones, one branch"
    assert push.priority_key(a, "origin", "main") != push.priority_key(a, "origin", "dev")
    assert push.priority_key(a, "nope", "main") != push.priority_key(b, "nope", "main"), "no URL: the repository"


def test_queued_checks_replace_pending_and_running_checks(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    started, child = tmp_path / "started", tmp_path / "child.pid"
    first = _detached_checks(run, capsys, "sh", "-c", f"echo $$ > {child}; touch {started}; sleep 60")
    try:
        assert _wait_for(started.exists)
        monkeypatch.setattr(push, "_boxed", lambda: True)
        cli.main(["checks", "--detach", "--", "false"])
        cli.main(["checks", "--detach", "--", "true"])
        assert len(list((p.state / detach.QUEUE).glob("*.json"))) == 1
        assert cli._checks_alive(cli._read_checks_pid(run)), "cancellation belongs to the daemon"
        assert cli._checks_result(run) == 1
        detach.tend(p)
        # the whole group got the signal; on a loaded host its members may take a moment to exit
        assert _wait_for(lambda: _gone_pid(first) and _gone_pid(int(child.read_text())))
        assert _wait_precheck(run) == "0", "the latest request replaces the pending one"
        assert cli._read_checks_pid(run)["cmd"] == ["--", "true"]
    finally:
        for pid in (first, int(child.read_text()) if child.exists() else 0):
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass


@pytest.mark.parametrize("failure", ["expired", "missing_worktree"])
def test_queued_checks_that_cannot_start_end_the_wait(env, tmp_path, monkeypatch, capsys, failure):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    (run / cli.CHECKS_RC).write_text("0\n")   # an old pass must not settle a replacement
    cli.main(["checks", "--detach", "--", "true"])
    assert cli._checks_result(run) == 1
    q = next((p.state / detach.QUEUE).glob("*.json"))
    m = json.loads(q.read_text())
    if failure == "expired":
        m["queued"] -= detach.QUEUED_S + 1
    else:
        m["checks"]["worktree"] = str(tmp_path / "gone")
    q.write_text(json.dumps(m))
    detach.tend(p)
    assert cli._checks_result(run) == 0
    assert (run / cli.CHECKS_RC).read_text().strip() == "killed"
    assert "not started" in capsys.readouterr().out
    assert not q.exists() and not (run / cli.CHECKS_PID).exists()


def test_a_request_queued_while_the_daemon_starts_an_older_one_is_kept_and_runs_last(env, tmp_path, monkeypatch):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", "true"])
    real, seen = cli._checks_start, []

    def start(a, *args):
        seen.append(a.cmd)
        if len(seen) == 1:   # a newer request arrives while the daemon cancels and starts the older one
            cli.main(["checks", "--detach", "--", "false"])
        return real(a, *args)

    monkeypatch.setattr(cli, "_checks_start", start)
    detach.tend(p)
    assert seen == [["--", "true"]]
    assert len(list((p.state / detach.QUEUE).glob("*.json"))) == 1, "the newer request was deleted"
    assert detach.queued(run / cli.CHECKS_RC).exists()
    assert _wait_precheck(run) == "0"   # the older checks finish meanwhile ...
    assert cli._checks_result(run) == 1, "... and must not settle the wait while the newer one is queued"
    detach.tend(p)
    assert seen[-1] == ["--", "false"] and cli._read_checks_pid(run)["cmd"] == ["--", "false"]
    assert not list((p.state / detach.QUEUE).glob("*.json")) and not detach.queued(run / cli.CHECKS_RC).exists()
    assert _wait_precheck(run) == "1"
    monkeypatch.setattr(push, "_boxed", lambda: False)
    assert cli._checks_result(run) == 0 and (run / cli.CHECKS_RC).read_text().strip() == "1"


def test_a_daemon_tick_between_the_queue_writes_leaves_no_orphan_marker(env, tmp_path, monkeypatch):
    import threading
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    starting = threading.Event()
    monkeypatch.setattr(cli, "_checks_start", lambda *args: starting.set() or os.getpid())
    monkeypatch.setattr(detach, "renice", lambda *args: None)
    real_write, ticks = detach.write_json, []

    def publish(path, data, **kwargs):
        real_write(path, data, **kwargs)
        # The daemon's own tick takes the request before enqueue writes its .queued marker.
        tick = threading.Thread(target=detach.tend, args=(p,))
        tick.start()
        ticks.append(tick)
        assert starting.wait(10)
        tick.join(2)   # it finishes here unless enqueue's lock holds its cleanup back until the marker is written

    monkeypatch.setattr(detach, "write_json", publish)
    cli.main(["checks", "--detach", "--", "true"])
    ticks[0].join(10)
    assert not ticks[0].is_alive()
    assert not list((p.state / detach.QUEUE).glob("*.json"))
    assert not detach.queued(run / cli.CHECKS_RC).exists(), "an orphan marker was left"
    (run / cli.CHECKS_RC).write_text("0\n")
    assert cli._checks_result(run) == 0, "a completed check must end the wait"


def test_a_sandboxed_probe_never_calls_live_host_checks_killed(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push, runner
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "pid_ns", lambda: "pid:[host]")   # macOS has none to record
    monkeypatch.setattr(push, "_boxed", lambda: True)
    started = tmp_path / "started"
    cli.main(["checks", "--detach", "--", "sh", "-c", f"touch {started}; sleep 30"])
    detach.tend(p)
    info = cli._read_checks_pid(run)
    try:
        assert _wait_for(started.exists) and cli._checks_alive(info) and info["pidns"] == push.pid_ns()
        with monkeypatch.context() as box:   # boxed: the host pid is not visible from here
            box.setattr(runner, "proc_start", lambda pid: None)
            assert cli._checks_result(run) == 1, "live host checks were declared killed from the sandbox"
        with monkeypatch.context() as box:   # another PID namespace, where the same pid is another process
            box.setattr(push, "_boxed", lambda: False)
            box.setattr(push, "pid_ns", lambda: "pid:[another]")
            box.setattr(runner, "proc_start", lambda pid: "another process's start")
            assert cli._checks_result(run) == 1
        assert not (run / cli.CHECKS_RC).exists(), "the executor's exit marker was overwritten"
        monkeypatch.setattr(push, "_boxed", lambda: False)
        assert cli._checks_result(run) == 1, "the host probe sees them running"
        os.killpg(info["pid"], signal.SIGTERM)
        assert _wait_precheck(run) == "143"
        monkeypatch.setattr(push, "_boxed", lambda: True)
        assert cli._checks_result(run) == 0, "a real exit code is reported from the sandbox too"
        assert "exit code 143" in capsys.readouterr().out
    finally:
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except OSError:
            pass


def test_a_failed_identity_write_ends_the_new_child_and_the_queued_wait(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    marker, children = tmp_path / "ran", []

    def full(path, data, **kw):
        children.append(data["pid"])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_json", full)
    monkeypatch.setattr(push, "_boxed", lambda: True)
    cli.main(["checks", "--detach", "--", f"touch {marker}"])
    detach.tend(p)
    assert children and _wait_for(lambda: _gone_pid(children[0])), "the child this launch made must end"
    assert not marker.exists() and not (run / cli.CHECKS_PID).exists()
    assert not list((p.state / detach.QUEUE).glob("*.json"))
    assert cli._checks_result(run) == 0 and "No space left" in capsys.readouterr().out


def test_status_does_not_warn_about_a_check_that_does_not_apply_on_the_branch(env, tmp_path):
    from ttp import push
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "work")
    (repo / "a.txt").write_text("")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    checks = push.check_list([{"run": "pytest tests/test_new.py", "if_exists": "tests/test_new.py"},
                              "pytest tests/test_gone.py"])
    assert push.unmatched_paths(repo, "origin", "work", checks)[1] == [
        "'pytest tests/test_gone.py': 'tests/test_gone.py'"]


def test_a_pr_fix_task_publishes_onto_the_prs_branch_whether_it_is_free_or_held(env, monkeypatch, capsys):
    """The daemon's PR-fix task delivers with `ttp push --own` onto the PR's branch, the finished task's
    ttp/t<id>-... branch: from a worktree on that branch when it is free, and from its own branch onto
    the PR's when another worktree holds it. Fast-forward only, never a new remote branch."""
    from ttp import push, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    url = "https://github.com/acme/widgets/pull/7"
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    pr_branch = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", pr_url=url, branch=pr_branch)
    _git_out(repo, "branch", pr_branch)
    other_wt = env["tmp"] / "pr"
    _git_out(repo, "worktree", "add", "-q", str(other_wt), pr_branch)
    _commit(other_wt, "pr.txt", "pr\n")
    _git_out(other_wt, "push", "-q", "origin", f"HEAD:refs/heads/{pr_branch}")
    _git_out(repo, "worktree", "remove", str(other_wt))
    d = Daemon(p.base)
    rec = {"task": src, "branch": pr_branch, "url": url}

    def publish(fix):
        path, branch = worktree.ensure(p, fix)
        _commit(path, f"fix{fix['id']}.txt", "fix\n")
        monkeypatch.chdir(path)
        monkeypatch.setenv("TTP_TASK", str(fix["id"]))
        assert push.resolve(p, path, own=True) == ("origin", pr_branch, True), branch
        assert _ttp("push", "--own") == 0, capsys.readouterr().err
        assert _git_out(origin, "rev-parse", pr_branch) == _git_out(path, "rev-parse", "HEAD")
        return path, branch

    # Free: the fix works on the PR's branch itself.
    fix, made = d.pr_findings_owner("acme/widgets#7", rec, "CI failing: tests")
    assert made and fix["branch"] == pr_branch
    path, branch = publish(fix)
    assert branch == pr_branch
    p.db.update_task(fix["id"], status="done")
    monkeypatch.chdir(repo)
    # Held: another worktree keeps the PR's branch; the fix's own branch is published onto the PR's.
    fix, made = d.pr_findings_owner("acme/widgets#7", rec, "CI failing: tests")
    assert made and fix["branch"] is None and f'"pr_branch:{pr_branch}"' in fix["labels"]
    assert "checked out elsewhere" in fix["spec"]
    path2, branch2 = publish(fix)
    assert branch2 == f"ttp/t{fix['id']}-fix-pr-findings-speed-up-the-loader"
    remote = _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert branch2 not in remote and pr_branch in remote
    # Never a rewrite: a head that does not fast-forward the PR's branch is refused before the checks.
    _git_out(path2, "reset", "-q", "--hard", "HEAD~2")
    _commit(path2, "other.txt", "x\n")
    tip = _git_out(origin, "rev-parse", pr_branch)
    assert _ttp("push", "--own") == 2 and "not an ancestor" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", pr_branch) == tip
    # Another task, neither carrying nor continuing the PR's branch, still may not publish it.
    monkeypatch.chdir(path)
    monkeypatch.setenv("TTP_TASK", str(p.db.add_task("unrelated", kind="code")))
    with pytest.raises(ValueError):
        push.own_target(p, path)


def test_follow_ups_and_redos_carry_a_branch_only_when_they_should(env):
    """A follow-up (`continues` on a done task) that names a finished task's branch but has no
    `pr_branch:` line carries no branch, so it lands on the push branch with `ttp push`; one with its
    own line carries that branch. A continuation of a failed task keeps the branch that task carried,
    unless its spec names only the push branch or the coordinator sets `pr_branch` to "" (task_add, or
    task_update before it starts): then it carries none (push.pr_branch_of)."""
    p = make(env)
    from ttp import coordinator as coord
    from ttp import push

    def task(title):
        return p.db.one("SELECT * FROM tasks WHERE title=?", (title,))

    def labels(title):
        return json.loads(task(title)["labels"])

    p.set_config("delivery.push_branch", "origin/proj")
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    rev = p.db.add_task("Review the loader", "s", kind="review")
    p.db.update_task(rev, status="done")
    # A follow-up naming the done task's branch as where its commit comes from: no label.
    assert coord.apply(p, [{"type": "task_add", "title": "Land the last commit", "kind": "code", "continues": rev,
                            "spec": f"Cherry-pick the last commit of {b} and land it on proj with `ttp push`."}]) == []
    assert labels("Land the last commit") == []
    assert push.pr_branch_of(p, task("Land the last commit")) == ""
    # A follow-up with its own line carries that branch.
    assert coord.apply(p, [{"type": "task_add", "title": "Fix the loader PR", "kind": "code", "continues": rev,
                            "spec": f"pr_branch: `{b}`\nFix the PR's CI."}]) == []
    assert labels("Fix the loader PR") == [f"pr_branch:{b}"]
    # A continuation of a failed task keeps the branch that task carried (it exists).
    subprocess.run(["git", "-C", str(p.root), "branch", "feature/x"], check=True, capture_output=True)
    gone = p.db.add_task("Fix the feature PR", "s", kind="code", labels=["pr_branch:feature/x"])
    p.db.update_task(gone, status="failed", branch=f"ttp/t{gone}-fix-the-feature-pr")
    assert coord.apply(p, [{"type": "task_add", "title": "Feature PR again", "kind": "code", "continues": gone,
                            "spec": "Finish the CI fix."}]) == []
    assert labels("Feature PR again") == [f"continues:{gone}"]
    assert push.pr_branch_of(p, task("Feature PR again")) == "feature/x"
    # A redo of a cancelled, mislabelled task whose spec names only the push branch carries none.
    bad = p.db.add_task("Land the entry", "s", kind="code", labels=[f"pr_branch:{b}"])
    p.db.update_task(bad, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the entry, redo", "kind": "code", "continues": bad,
                            "spec": "Land the entry on proj with `ttp push`; this task has no PR-branch label."}]) == []
    redo = task("Land the entry, redo")
    assert labels("Land the entry, redo") == [f"continues:{bad}", "no_pr_branch"]
    assert push.pr_branch_of(p, redo) == "" and push.pr_elsewhere(p, redo, p.config()["delivery"]) == ""
    assert any(f"#{redo['id']} continues #{bad} but not its branch {b}" in n for n in p.db.kv(coord.NOTES_KEY))
    # A branch never published (neither here nor on a remote) cannot be delivered onto: a redo that
    # does not name it carries none; one that names it keeps it.
    lost = p.db.add_task("Land the table", "s", kind="code", labels=["pr_branch:feature/never-made"])
    p.db.update_task(lost, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the table, redo", "kind": "code", "continues": lost,
                            "spec": "Land the table."},
                           {"type": "task_add", "title": "Make the table PR", "kind": "code", "continues": lost,
                            "spec": "Create feature/never-made with the table."}]) == []
    assert labels("Land the table, redo") == [f"continues:{lost}", "no_pr_branch"]
    assert any(f"#{task('Land the table, redo')['id']} continues #{lost} but not its branch feature/never-made "
               f"(never published)" in n for n in p.db.kv(coord.NOTES_KEY))
    assert labels("Make the table PR") == [f"continues:{lost}"]
    # Or the coordinator says so: pr_branch "" on task_add ...
    bad2 = p.db.add_task("Land the docs", "s", kind="code", labels=["pr_branch:feature/x"])
    p.db.update_task(bad2, status="cancelled")
    assert coord.apply(p, [{"type": "task_add", "title": "Land the docs, redo", "kind": "code", "continues": bad2,
                            "pr_branch": "", "spec": "Land the docs."}]) == []
    assert push.pr_branch_of(p, task("Land the docs, redo")) == ""
    # ... or on task_update before it starts, which also drops its own label; never once it runs.
    assert coord.apply(p, [{"type": "task_update", "id": task("Feature PR again")["id"], "pr_branch": ""},
                           {"type": "task_update", "id": task("Fix the loader PR")["id"], "pr_branch": ""}]) == []
    assert labels("Feature PR again") == [f"continues:{gone}", "no_pr_branch"]
    assert labels("Fix the loader PR") == ["no_pr_branch"]
    assert push.pr_branch_of(p, task("Feature PR again")) == ""
    p.db.update_task(task("Land the docs, redo")["id"], status="running")
    problems = coord.apply(p, [{"type": "task_update", "id": task("Land the docs, redo")["id"], "pr_branch": ""}])
    assert problems and "running" in problems[0]


def test_own_push_refuses_a_pr_branch_that_exists_nowhere(env, monkeypatch):
    """`ttp push --own` from a task's own branch publishes onto its pr_branch only when that branch
    exists here or on the remote; a missing one (e.g. a word parsed out of prose) or an invalid one is
    refused, and main/master never go."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])

    def target(label):
        t = p.db.add_task("Carry it", "s", kind="code", labels=[f"pr_branch:{label}"])
        own = f"ttp/t{t}-carry-it"
        _git_out(repo, "checkout", "-q", "-B", own)
        monkeypatch.setenv("TTP_TASK", str(t))
        return push.own_target(p, repo)

    with pytest.raises(ValueError, match="exists neither here nor on origin"):
        target("use")
    assert "use" not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    for bad in ("use that branch", "feat..ure"):
        with pytest.raises(ValueError, match="not one valid branch name"):
            target(bad)
    for shared in ("main", "master"):
        with pytest.raises(ValueError):
            target(shared)
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/remote")
    assert target("feature/remote") == ("origin", "feature/remote")
    _git_out(repo, "branch", "feature/local", "HEAD")
    assert target("feature/local") == ("origin", "feature/local")


def test_own_push_refuses_another_tasks_branch_it_only_inherited(env, monkeypatch, capsys):
    """A reported case, replayed: a plan task whose worktree started from a finished task's branch
    carried `pr_branch:<that branch>` without its spec asking for it, so `ttp push --own` targeted the
    other task's branch. Such an inherited branch of another task is refused, saying how to deliver
    instead (`ttp push --own-branch`, which publishes the own branch under its own name); a spec line
    `pr_branch: <branch>`, a continuation of the branch's owner and a review of an authorized task
    still publish onto it."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    src = p.db.add_task("Profile the loader", "s", kind="code")
    theirs = f"ttp/t{src}-profile-the-loader"
    p.db.update_task(src, status="done", branch=theirs)
    _git_out(repo, "checkout", "-q", "-b", theirs)
    _commit(repo, "theirs.txt", "theirs\n")
    _git_out(repo, "push", "-q", "origin", theirs)
    shipped = _git_out(origin, "rev-parse", theirs)

    def task(title, spec="s", kind="plan", labels=(), on=None):
        t = p.db.add_task(title, spec, kind=kind, labels=list(labels))
        own = f"ttp/t{t}-{title.lower().replace(' ', '-')}"
        p.db.update_task(t, status="running", branch=on or own)
        _git_out(repo, "checkout", "-q", "-B", on or own, theirs)
        _commit(repo, f"t{t}.txt", "work\n")
        monkeypatch.setenv("TTP_TASK", str(t))
        return t, on or own

    # The plan task on its own branch, labelled with the other task's branch: refused, nothing pushed.
    plan, own = task("Plan the cache", labels=[f"pr_branch:{theirs}"])
    with pytest.raises(ValueError, match="only inherited") as e:
        push.own_target(p, repo)
    assert f"task #{src}'s branch" in str(e.value) and "ttp push --own-branch --detach" in str(e.value)
    capsys.readouterr()
    assert _ttp("push", "--own") == 2
    assert "only inherited" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", theirs) == shipped
    # --own-branch publishes the task's own branch under its own name. cmd_push sets OWN_BRANCH_ENV in
    # this process: registered unset first, so teardown unsets it rather than restoring its value.
    monkeypatch.delenv(push.OWN_BRANCH_ENV, raising=False)
    assert _ttp("push", "--own-branch") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", own) == _git_out(repo, "rev-parse", "HEAD")
    assert _git_out(origin, "rev-parse", theirs) == shipped
    monkeypatch.delenv(push.OWN_BRANCH_ENV)
    # A worktree on the other task's branch itself (it started there): refused too.
    task("Plan again", labels=[f"pr_branch:{theirs}"], on=theirs)
    with pytest.raises(ValueError, match="only inherited"):
        push.own_target(p, repo)
    # Inherited through a continued task that only inherited it: refused.
    mid = p.db.add_task("Middle step", "s", kind="code", labels=[f"pr_branch:{theirs}"])
    p.db.update_task(mid, status="failed")
    task("Pick up the middle", kind="code", labels=[f"continues:{mid}", f"pr_branch:{theirs}"])
    with pytest.raises(ValueError, match="only inherited"):
        push.own_target(p, repo)
    # Allowed: a spec line naming it, a continuation of its owner, a review of an authorized task.
    named, _ = task("Fix its CI", spec=f"pr_branch: {theirs}\nFix CI there.", kind="code",
                    labels=[f"pr_branch:{theirs}"])
    assert push.own_target(p, repo) == ("origin", theirs)
    task("Finish the loader", kind="code", labels=[f"continues:{src}", f"pr_branch:{theirs}"])
    assert push.own_target(p, repo) == ("origin", theirs)
    rev = p.db.add_task(f"Review #{named}: Fix its CI", "s", kind="review", depends_on=[named],
                        labels=[f"pr_branch:{theirs}"])
    monkeypatch.setenv("TTP_TASK", str(rev))
    _git_out(repo, "checkout", "-q", f"ttp/t{named}-fix-its-ci")
    assert push.own_target(p, repo) == ("origin", theirs)


def test_a_continuation_does_not_inherit_a_third_tasks_branch_its_spec_does_not_name(env, monkeypatch):
    """A task_add continuing a task that only inherited another task's ttp/t<id>-... branch carries
    no branch (no_pr_branch) unless its own spec has a `pr_branch:` line naming it. The branch counts
    as published, so only its owner being another task keeps it off."""
    p = make(env)
    from ttp import coordinator as coord, push, worktree
    monkeypatch.setattr(push, "never_published", lambda *a, **k: False)
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    mid = p.db.add_task("Plan the cache", "s", kind="plan", labels=[f"pr_branch:{b}"])
    p.db.update_task(mid, status="failed")

    def labels(title):
        return json.loads(p.db.one("SELECT labels FROM tasks WHERE title=?", (title,))["labels"])

    assert coord.apply(p, [{"type": "task_add", "title": "Plan it again", "kind": "plan", "continues": mid,
                            "spec": "Redo the cache plan."}]) == []
    assert worktree.NO_PR_BRANCH in labels("Plan it again")
    again = p.db.one("SELECT id FROM tasks WHERE title='Plan it again'")["id"]
    assert any(f"#{again} continues #{mid} but not its branch {b} (#{src}'s branch" in n
               for n in p.db.kv(coord.NOTES_KEY))
    assert coord.apply(p, [{"type": "task_add", "title": "Fix the loader PR", "kind": "code", "continues": mid,
                            "spec": f"pr_branch: {b}\nFix the PR."}]) == []
    assert f"pr_branch:{b}" in labels("Fix the loader PR") and worktree.NO_PR_BRANCH not in labels("Fix the loader PR")


def test_the_daemon_clears_push_markers_of_dead_runs_and_logs_it(env):
    """A push marker left `running` by a run that died (its run lock free: killed, crashed, rebooted)
    is recorded as failed by the daemon's sweep (push.tend), which returns a line for its log; a
    finished marker past KEEP_S is removed, and a live push (its lock held) is left alone."""
    from ttp import push, locks
    p = make(env)
    folder = p.state / push.DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    dead = folder / "t603-20261013-022410-57783.json"
    dead.write_text(json.dumps({"id": dead.stem, "status": "running", "task": "603", "pid": 57783,
                                "target": "origin/ttp/t598-plan", "started": time.time() - 3600}))
    live = folder / "t604-20261013-022410-57784.json"
    live.write_text(json.dumps({"id": live.stem, "status": "running", "task": "604", "pid": os.getpid(),
                                "target": "origin/ttp/t604-x", "started": time.time()}))
    old = folder / "t1-20260101-000000-1.json"
    old.write_text(json.dumps({"id": old.stem, "status": "pushed", "ended": 0}))
    os.utime(old, (time.time() - push.KEEP_S - 60,) * 2)
    held = locks.try_take([push._run_lock(live)], "test", "test")
    try:
        lines = push.tend(p)
        assert push.tend(p) == [], "a marker is reported once"
    finally:
        held.close()
    assert json.loads(dead.read_text())["status"] == "failed"
    assert json.loads(live.read_text())["status"] == "running"
    assert not old.exists()
    assert any(f"cleared stale push marker {dead.name} of task 603 (origin/ttp/t598-plan)" in ln for ln in lines), lines
    assert any("removed 1 finished push marker" in ln for ln in lines), lines
    assert not [ln for ln in lines if live.name in ln]


def test_own_push_refuses_a_task_branch_other_than_the_one_its_run_was_given(env, monkeypatch, capsys):
    """A worktree that left the branch its task's run was given (the row's branch) for another of the
    task's own ttp/t<id>-... branches is refused by `ttp push --own`, naming both branches: it would
    otherwise publish the new branch's head onto the carried branch, which another task owns. On the
    given branch it publishes as before: onto the carried branch, or under its own name."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("First fix", "s", kind="code")
    carried = f"ttp/t{owner}-first-fix"
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{carried}")
    theirs = _git_out(origin, "rev-parse", carried)
    tid = p.db.add_task("Follow up", f"pr_branch: {carried}", kind="code", labels=[f"pr_branch:{carried}"])
    p.db.update_task(tid, status="running", branch=carried)
    monkeypatch.setenv("TTP_TASK", str(tid))
    _git_out(repo, "checkout", "-q", "-B", carried, theirs)
    assert push.own_target(p, repo) == ("origin", carried)   # the normal case: on the given branch
    new = f"ttp/t{tid}-new"
    _git_out(repo, "checkout", "-q", "-B", new)
    _commit(repo, "new.py", "x\n")
    with pytest.raises(ValueError, match=rf"{re.escape(new)} checked out.*delivers from {re.escape(carried)}"):
        push.own_target(p, repo)
    capsys.readouterr()
    assert _ttp("push", "--own") != 0
    err = capsys.readouterr().err
    assert new in err and carried in err, err
    assert _git_out(origin, "rev-parse", carried) == theirs
    assert new not in _git_out(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    # Given its own branch (the carried one was held elsewhere): that branch publishes onto the
    # carried one; a second own branch is refused, naming the given branch and where it goes.
    given = f"ttp/t{tid}-follow-up"
    p.db.update_task(tid, branch=given)
    with pytest.raises(ValueError, match=rf"delivers from {re.escape(given)}, which it publishes onto {re.escape(carried)}"):
        push.own_target(p, repo)
    _git_out(repo, "checkout", "-q", "-B", given)
    assert push.own_target(p, repo) == ("origin", carried)
    # A task with no pr_branch: its given branch goes under its own name, another of its own does not.
    plain = p.db.add_task("Plain", "s", kind="code")
    p.db.update_task(plain, status="running", branch=f"ttp/t{plain}-plain")
    monkeypatch.setenv("TTP_TASK", str(plain))
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-plain")
    assert push.own_target(p, repo) == ("origin", f"ttp/t{plain}-plain")
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-other")
    with pytest.raises(ValueError, match=f"task #{plain} delivers from ttp/t{plain}-plain"):
        push.own_target(p, repo)
    # Its notes branch still publishes itself.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{plain}-notes")
    assert push.own_target(p, repo) == ("origin", f"ttp/t{plain}-notes")


def test_own_push_publishes_a_fresh_own_branch_made_off_an_inherited_one(env, monkeypatch):
    """A task given another task's branch it only inherited (through `continues`, no `pr_branch:` line
    in its spec) whose spec asks for a fresh branch publishes its own ttp/t<id>-... branch under its
    own name with `ttp push --own`, never onto the inherited one. Another task's branch stays refused,
    and so does a second own branch when the given one is the task's own."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    owner = p.db.add_task("Earlier", "s", kind="code")
    inherited = f"ttp/t{owner}-earlier"
    _git_out(other, "push", "-q", "origin", f"HEAD:refs/heads/{inherited}")
    theirs = _git_out(origin, "rev-parse", inherited)
    p.db.update_task(owner, status="done", branch=inherited)
    tid = p.db.add_task("Fresh", "start a new ttp/* branch from the named base", kind="code",
                        labels=[f"continues:{owner}", f"pr_branch:{inherited}"])
    p.db.update_task(tid, status="running", branch=inherited)
    monkeypatch.setenv("TTP_TASK", str(tid))
    fresh = f"ttp/t{tid}-from-base"
    _git_out(repo, "checkout", "-q", "-B", fresh)
    _commit(repo, "fresh.py", "x\n")
    assert push.own_target(p, repo) == ("origin", fresh)
    assert _git_out(origin, "rev-parse", inherited) == theirs
    # Another task's branch checked out is still not this task's to publish.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{owner + 1000}-elsewhere")
    with pytest.raises(ValueError, match="only this task's own branch"):
        push.own_target(p, repo)
    # Its spec naming the inherited branch makes it the one to deliver on: the fresh branch is refused.
    p.db.update_task(tid, spec=f"pr_branch: {inherited}")
    _git_out(repo, "checkout", "-q", fresh)
    with pytest.raises(ValueError, match=rf"{re.escape(fresh)} checked out.*delivers from {re.escape(inherited)}"):
        push.own_target(p, repo)


def test_a_review_publishes_the_pr_branch_its_subject_carries_and_nothing_else(env, monkeypatch, capsys):
    """A review whose subject task (named by the `Review #<id>` title or its depends_on) carries a
    pr_branch that is another task's ttp/t<id>-... branch publishes that branch with `ttp push --own`
    from a worktree on it, fast-forward only. Any other task's branch is still refused, and a head
    the remote already has is reported as pushed without running the checks or pushing again."""
    from ttp import push
    log = env["tmp"] / "checked"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"git rev-parse HEAD >> {log}"])
    src = p.db.add_task("Fix the mask", "s", kind="code")
    b = f"ttp/t{src}-fix-the-mask"
    p.db.update_task(src, status="done", branch=b)
    pub = p.db.add_task("Publish the mask fix", f"pr_branch: {b}\nPublish it.", kind="code",
                        labels=[f"pr_branch:{b}"])
    p.db.update_task(pub, status="done", branch=b)
    by_title = p.db.add_task(f"Review #{pub}: Publish the mask fix", "s", kind="review", labels=["review_only"])
    by_dep = p.db.add_task("Check the publish", "s", kind="review", depends_on=[pub])
    _git_out(repo, "checkout", "-q", "-B", b)
    _commit(repo, "mask.py", "fixed\n")   # local only, as the finished task left it
    head = _git_out(repo, "rev-parse", "HEAD")
    for rev in (by_title, by_dep):
        monkeypatch.setenv("TTP_TASK", str(rev))
        assert push.own_target(p, repo) == ("origin", b), rev
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", b) == head
    assert log.read_text().split() == [head]
    # Already there: success, with no checks and no push.
    capsys.readouterr()
    assert _ttp("push", "--own") == 0
    assert "nothing to push" in capsys.readouterr().err
    assert log.read_text().split() == [head]
    # An unrelated task's branch is refused, for a review of any subject and for a non-review.
    stray = p.db.add_task("Something else", "s", kind="code", labels=["pr_branch:feature/x"])
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{stray}-something-else")
    _commit(repo, "stray.py", "x\n")
    plain = p.db.add_task("Not a review", "s", kind="code", depends_on=[pub])
    for task in (by_title, by_dep, plain):
        monkeypatch.setenv("TTP_TASK", str(task))
        with pytest.raises(ValueError, match="publishes only this task's own branch"):
            push.own_target(p, repo)
    # A review's subject carrying the branch is what lets it through: a non-review never publishes it.
    _git_out(repo, "checkout", "-q", b)
    monkeypatch.setenv("TTP_TASK", str(plain))
    with pytest.raises(ValueError, match="publishes only this task's own branch"):
        push.own_target(p, repo)
    assert _git_out(origin, "rev-parse", b) == head


def test_a_task_carrying_a_branch_works_on_it_in_a_worktree_and_publishes_onto_it(env, monkeypatch, capsys):
    """A task labelled pr_branch:<branch>, of any kind but review or harness, runs in a worktree on that
    branch (made from the remote's when there is no local one) and `ttp push --own` fast-forwards it.
    When it cannot check the branch out (another worktree holds it, or it is a shared branch), the task
    works on its own branch from that branch's head."""
    from pathlib import Path
    from ttp import prompts, push, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    src = p.db.add_task("Speed up the loader", "s", kind="code")
    b = f"ttp/t{src}-speed-up-the-loader"
    p.db.update_task(src, status="done", branch=b)
    _git_out(repo, "branch", b)
    wt = env["tmp"] / "pr"
    _git_out(repo, "worktree", "add", "-q", str(wt), b)
    _commit(wt, "pr.txt", "pr\n")
    _git_out(wt, "push", "-q", "origin", f"HEAD:refs/heads/{b}")
    _git_out(repo, "worktree", "remove", str(wt))
    _git_out(repo, "branch", "-D", b)   # only the remote has it now
    d = Daemon(p.base)
    task = p.db.task(p.db.add_task("Answer the PR comments", "s", kind="work",
                                   labels=[f"continues:{src}", f"pr_branch:{b}"]))
    cwd, branch = d._workdir_for(task)
    assert Path(cwd) == p.worktrees / f"t{task['id']}" and branch == b
    assert _git_out(cwd, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", b)
    assert f"delivers onto branch {b}" in prompts.worker_task(p, task, cwd, branch)
    _commit(Path(cwd), "fix.txt", "fix\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("TTP_TASK", str(task["id"]))
    assert push.resolve(p, Path(cwd), own=True) == ("origin", b, True)
    assert _ttp("push", "--own") == 0, capsys.readouterr().err
    assert _git_out(origin, "rev-parse", b) == _git_out(cwd, "rev-parse", "HEAD")
    # Held by that worktree: the next one works on its own branch from the carried branch's head.
    monkeypatch.chdir(repo)
    held = p.db.task(p.db.add_task("More PR comments", f"pr_branch: `{b}`", kind="work",
                                   labels=[f"pr_branch:{b}"]))
    cwd2, branch2 = d._workdir_for(held)
    assert branch2 == f"ttp/t{held['id']}-more-pr-comments"
    assert _git_out(cwd2, "rev-parse", "HEAD") == _git_out(origin, "rev-parse", b)
    monkeypatch.setenv("TTP_TASK", str(held["id"]))
    assert push.resolve(p, Path(cwd2), own=True) == ("origin", b, True)
    # A shared branch is never checked out as the task's branch.
    shared = p.db.task(p.db.add_task("On the push branch", "s", kind="code", labels=["pr_branch:proj"]))
    assert worktree.on_carried(p, shared) == ""
    assert d._workdir_for(shared)[1] == f"ttp/t{shared['id']}-on-the-push-branch"
    # A review still works in the change's worktree.
    review = p.db.task(p.db.add_task("Review it", "s", kind="review", labels=[f"pr_branch:{b}"]))
    assert d._workdir_for(review) == (str(p.root), None)


# Wordings of the usual delivery rules (the review pushes, not the worker): none is a ban on pushing.
USUAL_PUSH_RULES = ("Do not push.", "Never push to the push branch yourself.",
    "Do not push to work directly; the review pushes it.", "Never push to main.",
    "Never push to the push branches of other projects.",
    "Only a change whose whole diff is kept off the push branch by "
    "delivery.push_exclude_paths is review only.",
    "With the push queue on, workers never push to the push branch; the daemon does.",
    "Do not push to the push branch: the review pushes it.",
    "Workers must not push to the push branch themselves.",
    "Fix: a notes commit cannot reach the push branch, so drop it.",
    "Never push it to the push branch yourself.",
    # Prose is never a ban, not even one naming the change: only the marker line.
    "Write the notes. They must not be pushed to the push branch.",
    "Never push this to work.", "This change must not reach the push branch.",
    "These notes must not be pushed to work.", "Keep this change off origin/work.",
    "or (b) the code task's spec or hand-off says the change must not be pushed "
    "to the push branch",
    "A review of a change whose spec says it must not reach the push branch is "
    "review only.",
    "Detect when the hand-off says this change must not be pushed to the push branch.",
    "If it must not reach the push branch, the review is review only.",
    'Such a spec names the change as what is banned ("this change must not be '
    'pushed to the push branch")',
    "Rely on one explicit spec marker on its own line, e.g. a line starting "
    "`no_push:` (the reason follows).",
    "no_push: false", "Set no_push: true in the hand-off.")


def test_the_review_of_a_change_that_must_not_reach_the_push_branch_has_no_push_step(env, monkeypatch):
    # A notes-only change (every file push-excluded), or one its spec or hand-off keeps off the push
    # branch, gets a review-only spec: no `ttp push` and no push-queue approval, with the reason.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)   # a repo with no remote never pushes: review only
    p.set_config("delivery.push_branch", "work")
    p.set_config("delivery.push_exclude_paths", ["notes", "*.log"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject

    def review(title, files, spec=None, result=None):
        before = (lambda tid, head: p.db.update_task(tid, spec=spec)) if spec else None
        _, _, _, (rev,) = _finish_code(env, p, title, files, result=result, before=before)
        return rev

    def review_only(rev):
        return "ttp push` from" not in rev["spec"] and "Review only" in rev["spec"] \
            and push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and rev["spec"].rstrip().endswith("findings.")

    def pushes(rev):
        return "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])

    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        rev = review(f"notes {on}", {"notes/a.md": 5, "run.log": 2})
        assert review_only(rev) and "delivery.push_exclude_paths keeps off" in rev["spec"], (on, rev["spec"])
        assert pushes(review(f"mixed {on}", {"notes/b.md": 5, "app.py": 3})), on
        rev = review(f"forbidden {on}", {"c.py": 3}, spec="Write the notes.\nno_push: notes stay local\n")
        assert review_only(rev) and "notes stay local" in rev["spec"], on
        for i, ban in enumerate(("no_push:", "  no_push: `a local tool`", "Fix it.\n\nno_push: true\nThen stop.")):
            assert review_only(review(f"ban {on} {i}", {f"d{i}.py": 3}, spec=ban)), (on, ban)
        rev = review(f"flag {on}", {"e.py": 3}, result={"status": "done", "summary": "ok", "no_push": "a local tool"})
        assert review_only(rev) and "a local tool" in rev["spec"], on
        # The usual rule that the review pushes, not the worker, is not a prohibition. The review's
        # push step follows push.kept_off of the code task alone (the daemon passes it the task and its
        # diff), so each wording is checked there and two of them go through a whole review.
        d = p.config()["delivery"]
        for i, usual in enumerate(USUAL_PUSH_RULES):
            task = p.db.task(p.db.add_task(f"usual {on} {i}", usual, kind="code", origin="coordinator"))
            assert push.kept_off(task, {f"g{i}.py": 3}, d, p) == "", (on, usual)
        for i in (0, USUAL_PUSH_RULES.index("This change must not reach the push branch.")):
            assert pushes(review(f"usual {on} {i}", {f"g{i}.py": 3}, spec=USUAL_PUSH_RULES[i])), (on, i)
        rev = review(f"summary {on}", {"f.py": 3},
                     result={"status": "done", "summary": "Done; keep it off the push branch."})
        assert pushes(rev), on
    # No push branch: review only as before, without the reason.
    p.set_config("delivery.push_branch", "")
    rev = review("no branch", {"notes/c.md": 5})
    assert "Review only" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])


def test_kept_off_explicit_no_push_false_in_a_continuation_ends_the_walk(env, monkeypatch):
    # `no_push: false` in a continuation's spec, or no_push false in its hand-off, lifts the
    # ancestor's ban for it and for its own continuations; silence still inherits.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "work")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    d = p.config()["delivery"]

    def code(title, spec="", labels=None, result=None):
        tid = p.db.add_task(title, spec, kind="code", origin="coordinator", labels=labels)
        if result:
            p.db.update_task(tid, result=json.dumps(result))
        return p.db.task(tid)

    root = code("notes", "no_push: notes stay local\n")
    lifted = code("continue: notes", "Go on.\nno_push: false\n", labels=[f"continues:{root['id']}"])
    assert push.kept_off(lifted, None, d, p) == ""
    assert push.kept_off(code("fix", "", labels=[f"continues:{lifted['id']}"]), None, d, p) == ""
    for flag in (False, "false", 0):
        h = code("handed", "", labels=[f"continues:{root['id']}"],
                 result={"status": "done", "summary": "ok", "no_push": flag})
        assert push.kept_off(h, None, d, p) == "", flag
    assert "notes stay local" in push.kept_off(code("quiet", "", labels=[f"continues:{root['id']}"]), None, d, p)


def test_kept_off_inherits_no_push_from_the_tasks_it_continues(env, monkeypatch):
    # A continuation (a `continue:` task, or a fix of a review's findings) keeps the nearest ancestor's
    # no_push: its review is review only too. A chain without the marker stays pushable; a cycle ends.
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "work")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    d = p.config()["delivery"]

    def code(title, spec="", labels=None, result=None):
        tid = p.db.add_task(title, spec, kind="code", origin="coordinator", labels=labels)
        if result:
            p.db.update_task(tid, result=json.dumps(result))
        return p.db.task(tid)

    root = code("notes", "Write the notes.\nno_push: notes stay local\n")
    cont = code("continue: notes", "Go on.", labels=[f"continues:{root['id']}"])
    fix = code("Fix review #9: notes", "", labels=[f"continues:{cont['id']}", "review_fix:9"])
    why = push.kept_off(fix, {"a.py": 1}, d, p)
    assert "notes stay local" in why and f"#{root['id']} (which it continues)" in why, why
    assert "notes stay local" in push.kept_off(cont, None, d, p)
    assert push.kept_off(fix, None, d) == ""   # without the project only its own marker counts
    flagged = code("tool", "", result={"status": "done", "summary": "ok", "no_push": "a local tool"})
    assert "a local tool" in push.kept_off(code("more", "", labels=[f"continues:{flagged['id']}"]), None, d, p)
    # The nearest ancestor's reason wins; the task's own marker before any.
    mid = code("mid", "no_push: nearer", labels=[f"continues:{root['id']}"])
    assert "nearer" in push.kept_off(code("leaf", "", labels=[f"continues:{mid['id']}"]), None, d, p)
    assert "its spec" in push.kept_off(code("own", "no_push: mine", labels=[f"continues:{root['id']}"]), None, d, p)
    # No marker anywhere in the chain: pushable. A cycle of continues labels ends.
    plain = code("plain", "Just code.")
    assert push.kept_off(code("next", "", labels=[f"continues:{plain['id']}"]), None, d, p) == ""
    a = code("loop a", "")
    b = code("loop b", "", labels=[f"continues:{a['id']}"])
    p.db.update_task(a["id"], labels=json.dumps([f"continues:{b['id']}"]))
    assert push.kept_off(p.db.task(a["id"]), None, d, p) == ""
    # End to end: the review of a fix of a no_push task is review only; that of a plain chain pushes.
    _, _, _, (rev,) = _finish_code(env, p, "fix of notes", {"b.py": 3},
                                   labels=[f"continues:{root['id']}", "review_fix:9"])
    assert "Review only" in rev["spec"] and "notes stay local" in rev["spec"], rev["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and "ttp push` from" not in rev["spec"]
    _, _, _, (rev,) = _finish_code(env, p, "fix of plain", {"c.py": 3}, labels=[f"continues:{plain['id']}"])
    assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])


def test_allow_protected_push_branch_lets_reviews_push_to_main_and_without_it_they_never_git_push(env, monkeypatch, capsys):
    from ttp import cli, push, pushq
    from ttp.project import config_problems
    p = make(env)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    for ref in ("main", "origin/master", "refs/heads/main"):
        assert push.push_branch_problem(ref, allow=True) == "", ref
        assert "allow_protected_push_branch" in push.push_branch_problem(ref)
    assert "always refuses" in push.push_branch_problem("HEAD", allow=True)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "main")

    def review(title):
        _, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3})
        return rev

    rev = review("off")
    assert "Review only" in rev["spec"] and "never fall back to a raw `git push` to main" in rev["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and "ttp push` from" not in rev["spec"]
    cli.main(["doctor", p.name])
    out = capsys.readouterr().out
    assert "delivery.allow_protected_push_branch" in out and "raw `git push`" in out
    p.set_config("delivery.allow_protected_push_branch", True)
    assert not any("push_branch" in x for x in config_problems(p.raw_config(), p.root))
    rev = review("on")
    assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    # The prompts say the same: the reviewer never falls back to git push; only the user's word sets the key.
    text = (RUNTIME.parent / "template" / "prompts" / "kind-review.md").read_text()
    assert "NEVER fall back to a raw `git push` to that branch" in text
    coordinator = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    rule = next(line for line in coordinator.splitlines() if line.startswith("| `config_set`"))
    assert "`delivery.allow_protected_push_branch`" in rule and rule.index("allow_protected") < rule.index("only on the user's word")
    assert "never a raw `git push` to it" in coordinator


def test_a_push_branch_no_push_can_reach_is_flagged_and_its_reviews_are_review_only(env, monkeypatch, capsys):
    # delivery.push_branch names main/master (ttp push always refuses it), or the code repo has no
    # remote: every review push would exit 2. The config check and doctor say so, and the review
    # spec has no push step.
    from ttp import cli, push, pushq
    from ttp.project import config_problems
    p = make(env)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    # No repo to ask: only the name, its first part read either way.
    for ref in ("main", "master", "origin/main", "upstream/master", "refs/heads/main", "origin/refs/heads/master"):
        assert "always refuses" in push.push_branch_problem(ref), ref
    for ref in ("", None, "work", "origin/work", "maintenance", "main-next"):
        assert push.push_branch_problem(ref) == "", ref
    # A repo with no remote: any push branch.
    assert "no git remote" in push.push_branch_problem("work", p.root)
    assert any("no git remote" in x for x in config_problems({"delivery": {"push_branch": "work"}}, p.root))
    assert config_problems({"delivery": {"push_branch": "work"}}) == []
    # Not a git repo: only the name.
    assert push.push_branch_problem("work", env["tmp"] / "nowhere") == ""

    def review(title):
        _, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3})
        return rev

    def review_only(rev, why):
        return "ttp push` from" not in rev["spec"] and "Review only" in rev["spec"] and why in rev["spec"] \
            and push.REVIEW_ONLY_LABEL in json.loads(rev["labels"]) and rev["spec"].rstrip().endswith("findings.")

    p.set_config("delivery.push_branch", "work")
    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        assert review_only(review(f"noremote{on}"), "no git remote"), on
    cli.main(["doctor", p.name])
    assert "project.json: delivery.push_branch: the code repo has no git remote" in capsys.readouterr().out
    # With a remote: a protected branch, bare or behind the remote's name, never pushes either.
    _with_origin(env, clone=False)
    _git_out(p.root, "remote", "add", "upstream", str(env["tmp"] / "remote.git"))
    for i, ref in enumerate(("main", "origin/master", "upstream/main")):
        p.set_config("delivery.push_branch", ref)
        assert review_only(review(f"protected{i}"), "a branch ttp push always refuses"), ref
        assert any("always refuses" in x for x in config_problems(p.raw_config(), p.root)), ref
    cli.main(["doctor", p.name])
    assert "project.json: delivery.push_branch: upstream/main is main, a branch ttp push always refuses" \
        in capsys.readouterr().out
    # A remote the name does not use: origin is the fallback, and missing it never pushes.
    p.set_config("delivery.push_branch", "work")
    _git_out(p.root, "remote", "remove", "origin")
    assert review_only(review("noorigin"), "which is not a git remote")
    # A branch of its own on a remote the repo has: the review pushes as before; "team/main" is a
    # branch of origin, not main.
    _git_out(p.root, "remote", "add", "origin", str(env["tmp"] / "remote.git"))
    for i, ref in enumerate(("work", "upstream/work", "team/main")):
        p.set_config("delivery.push_branch", ref)
        rev = review(f"ok{i}")
        assert "ttp push` from" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"]), ref
        assert not any("push_branch" in x for x in config_problems(p.raw_config(), p.root)), ref


def test_the_push_queue_refuses_a_head_carrying_commits_of_a_failed_review_unless_its_review_accepts_them(
        env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    x, xbranch, xhead, _ = _pq_stacked(s, "rejected", review="failed")
    # Stacked on the rejected branch: refused, naming the task and its commit.
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=xbranch, review="running")
    check = pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead}])
    assert f"carries unreviewed commits from #{x}," in check.get("invalid", ""), check
    assert "edit rejected.txt" in check["invalid"] and '"inherited": [%d]' % x in check["invalid"]
    # The review judged them and lists the task: approved, and the feed line says so.
    check = pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead, "inherited": [x]}])
    assert check.get("entries") and check["inherited"] == {chead: [x]}, check
    assert f"carries commits of #{x}" in pushq.queued_text(s.p.db.task(crev), check)
    # A rebased copy of the rejected commit (another sha, the same patch) is caught too.
    d, dbranch, dhead, drev = _pq_stacked(s, "copied", picks=[xhead], review="running")
    assert _git_out(s.path, "rev-parse", f"{dbranch}~1") != xhead
    check = pushq.check_approval(s.p, s.p.db.task(drev), [{"branch": dbranch, "head": dhead}])
    assert f"from #{x}," in check.get("invalid", ""), check
    # The rejected task's own re-review (or a fix that continues it) covers it: not refused.
    rr = s.p.db.add_task("re-review", "r", kind="review", origin="coordinator", depends_on=[x])
    assert pushq.check_approval(s.p, s.p.db.task(rr), [{"branch": xbranch, "head": xhead}]).get("entries")
    # A later passing review of the rejected task clears it.
    s.p.db.update_task(rr, status="done")
    assert pushq.check_approval(s.p, s.p.db.task(crev), [{"branch": cbranch, "head": chead}]).get("entries")
    # An unrelated head is approved as before.
    assert pushq.check_approval(s.p, s.p.db.task(s.review), [{"branch": s.branch, "head": s.head}]).get("entries")


def test_the_push_queue_accepts_a_head_stacked_on_a_passed_branch_landed_or_not(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    p_, pbranch, phead, prev = _pq_stacked(s, "passed", review="done")
    # A rejected sibling stacked on the passed branch holds its commits too: they stay the passed task's.
    _pq_stacked(s, "sibling", on=pbranch, review="failed")
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=pbranch, review="running")
    entries = [{"branch": cbranch, "head": chead}]
    check = pushq.check_approval(s.p, s.p.db.task(crev), entries)
    assert check.get("entries") and "inherited" not in check, check
    # Landed as a rebased copy (as a batch lands it) and its review gone: its patch is on the tip.
    s.p.db.update_task(prev, status="cancelled")
    _git_out(s.other, "fetch", "-q", str(s.repo), pbranch)
    _git_out(s.other, "cherry-pick", "-x", "FETCH_HEAD")
    _git_out(s.other, "push", "-q", "origin", "HEAD:proj")
    _git_out(s.repo, "fetch", "-q", "origin")
    assert _git_out(s.repo, "rev-parse", "origin/proj") != phead
    check = pushq.check_approval(s.p, s.p.db.task(crev), entries)
    assert check.get("entries"), check


def test_a_daemon_queued_review_names_the_rejected_commits_its_head_carries(env, monkeypatch):
    from ttp import pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, head: None)
    x, xbranch, _, _ = _pq_stacked(s, "rejected", review="failed")
    c, cbranch, chead, _ = _pq_stacked(s, "stacked", on=xbranch)
    d = Daemon(s.p.base)
    rid, added = d._auto_review(s.p.db.task(c), "made the change")
    spec = s.p.db.task(rid)["spec"]
    assert added and f"carries unreviewed commits from tasks whose latest review failed (#{x}: " in spec, spec
    assert "edit rejected.txt" in spec and '"inherited": [%d]' % x in spec
    # What the spec names is what the queue would refuse the review's approval for, and accept once listed.
    entry = {"branch": cbranch, "head": chead}
    assert f"from #{x}," in pushq.check_approval(s.p, s.p.db.task(rid), [entry]).get("invalid", "")
    assert pushq.check_approval(s.p, s.p.db.task(rid), [{**entry, "inherited": [x]}]).get("entries")
    # A head with no rejected commits under it: no such line.
    f, _, _, _ = _pq_stacked(s, "fresh")
    rid, added = d._auto_review(s.p.db.task(f), "made the change")
    assert added and "unreviewed commits" not in s.p.db.task(rid)["spec"]


def _pq_add_reviewed_tasks(db, start, stop):
    """Code tasks start..stop-1 on branches of their own, each with a finished review naming its branch
    in the spec (passed and failed in turn)."""
    with db.conn:
        for i in range(start, stop):
            db.conn.execute("INSERT INTO tasks (created, updated, title, spec, kind, status, branch) "
                            "VALUES (?, ?, ?, '', 'code', 'done', ?)",
                            (time.time(), time.time(), f"t{i}", f"ttp/t{9000 + i}-change-{i}"))
            db.conn.execute("INSERT INTO tasks (created, updated, title, spec, kind, status) "
                            "VALUES (?, ?, ?, ?, 'review', ?)",
                            (time.time(), time.time(), f"check {i}",
                             f"Check ttp/t{9000 + i}-change-{i} against the spec.", ("done", "failed")[i % 2]))


def test_the_inherited_commit_check_stays_fast_with_many_tasks_and_reviews(env, monkeypatch):
    # check_approval runs in the daemon's result path: its work must not grow with tasks x reviews.
    # Counted, not timed against the clock (a loaded machine makes any wall-clock bound flaky): git
    # calls stay the same however many tasks there are, rows read from the database grow linearly,
    # and its own CPU time grows sub-quadratically from n to 8n tasks and reviews.
    from ttp import pushq
    from ttp.db import DB
    s = _pq(env, monkeypatch)
    x, xbranch, _, _ = _pq_stacked(s, "rejected", review="failed")
    c, cbranch, chead, crev = _pq_stacked(s, "stacked", on=xbranch, review="running")
    entries = [{"branch": cbranch, "head": chead}]
    n = {"git": 0, "rows": 0}
    run, q, one = subprocess.run, DB.q, DB.one

    def counted_run(*a, **kw):
        n["git"] += 1
        return run(*a, **kw)

    def counted_q(self, *a, **kw):
        rows = q(self, *a, **kw)
        n["rows"] += len(rows)
        return rows

    def counted_one(self, *a, **kw):
        n["rows"] += 1
        return one(self, *a, **kw)

    def measure():
        task = s.p.db.task(crev)
        took = []
        for _ in range(3):
            n.update(git=0, rows=0)
            with monkeypatch.context() as m:
                m.setattr(pushq.subprocess, "run", counted_run)
                m.setattr(DB, "q", counted_q)
                m.setattr(DB, "one", counted_one)
                t0 = time.process_time()
                check = pushq.check_approval(s.p, task, entries)
                took.append(time.process_time() - t0)
            assert f"carries unreviewed commits from #{x}," in check.get("invalid", ""), check
        return dict(n, cpu=min(took))

    _pq_add_reviewed_tasks(s.p.db, 0, 100)
    small = measure()
    _pq_add_reviewed_tasks(s.p.db, 100, 800)
    big = measure()
    assert big["git"] == small["git"], f"git calls grew with the tasks: {small} -> {big}"
    assert big["rows"] - small["rows"] <= 10 * 700, f"rows read grew faster than the tasks: {small} -> {big}"
    # 8x the tasks: linear work grows at most 8x (much less, as git dominates), quadratic 64x.
    assert big["cpu"] < 16 * small["cpu"], f"CPU time grew quadratically: {small} -> {big}"


def test_a_review_only_stack_stays_review_only_through_its_fix_and_the_push_queue_ignores_its_approval(
        env, monkeypatch):
    from ttp import push, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    entries = [{"branch": s.branch, "head": s.head}]
    assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries"), "approved"
    s.p.db.update_task(s.review, labels=[push.REVIEW_ONLY_LABEL])
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert "review only" in check.get("ignored", ""), check
    # A failed review-only review: its fix is told not to push, its re-review stays review only.
    s.p.db.update_task(s.review, status="failed")
    d = Daemon(s.p.base)
    out = d._fix_failed_review(s.p.db.task(s.review), [{"title": "bug", "spec": "fix it"}], "one bug")
    assert out, "no fix queued"
    fid, rid, _ = out
    assert "do not push it" in s.p.db.task(fid)["spec"] and "Leave the push" not in s.p.db.task(fid)["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(s.p.db.task(rid)["labels"])
    # Its checks are not started for the push queue either.
    started = []
    monkeypatch.setattr(Daemon, "_precheck", lambda self, task, full: started.append(task["id"]))
    s.p.db.update_task(fid, branch=s.branch)
    d._precheck_open(s.p.db.task(fid), s.p.db.task(rid))
    assert started == []


def test_the_re_review_of_a_pr_branch_stack_carries_its_pr_branch_or_no_push_step(env, monkeypatch):
    """A code task labelled pr_branch:<branch> whose review fails: the daemon's fix and re-review keep
    delivering onto that branch. The re-review carries the label and says to publish the fix's head
    from the fix's branch, so `ttp push --own` from there (or from its own branch) publishes onto
    the pr_branch. A re-review that cannot carry it (the stack carries no pr_branch now, or own
    pushes are off) has no push step and never mentions --own."""
    from ttp import push, pushq, worktree
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/feature/x")

    def failed_stack(title):
        for t in p.db.q("SELECT id FROM tasks WHERE status NOT IN ('done','failed','cancelled')"):
            p.db.update_task(t["id"], status="cancelled")   # one stack at a time
        cid, _, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 3}, labels=["pr_branch:feature/x"])
        assert "`ttp push --own --detach`" in rev["spec"], rev["spec"]
        assert "pr_branch:feature/x" in json.loads(rev["labels"])
        p.db.update_task(rev["id"], status="failed")
        return cid, rev

    def fix_and_re_review(rev):
        out = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
        assert out, "no fix queued"
        return p.db.task(out[0]), p.db.task(out[1])

    cid, rev = failed_stack("one")
    fix, rr = fix_and_re_review(rev)
    labels = json.loads(rr["labels"])
    assert "pr_branch:feature/x" in labels and push.REVIEW_ONLY_LABEL in labels, labels
    assert (f"publish #{fix['id']}'s head onto its pr_branch feature/x with `ttp push --own --detach` from "
            f"#{fix['id']}'s worktree (branch {fix['branch']})") in rr["spec"], rr["spec"]
    # The fix leaves the push to the re-review.
    assert "do not push it" in fix["spec"] and "--own" not in fix["spec"]
    path, branch = worktree.ensure(p, fix)
    _commit(path, "fixed.py", "fixed\n")
    monkeypatch.setenv("TTP_TASK", str(rr["id"]))
    assert push.own_target(p, path) == ("origin", "feature/x")
    # From a branch of its own, too: the label is what it publishes onto.
    _git_out(path, "checkout", "-q", "-b", f"ttp/t{rr['id']}-re-review")
    assert push.own_target(p, path) == ("origin", "feature/x")

    # The stack no longer carries a pr_branch: no label, and the first review's --own step is left out.
    _, rev = failed_stack("two")
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND title='two'"):
        p.db.update_task(t["id"], labels=[worktree.NO_PR_BRANCH])
    _, rr = fix_and_re_review(rev)
    assert not any(lb.startswith("pr_branch:") for lb in json.loads(rr["labels"])), rr["labels"]
    assert "--own" not in rr["spec"] and "Review only" in rr["spec"], rr["spec"]
    assert "carries no pr_branch" in rr["spec"]

    # A first review older than its pr_branch label: its own step sentence still counts.
    _, rev = failed_stack("legacy")
    p.db.update_task(rev["id"], labels=[lb for lb in json.loads(rev["labels"]) if not lb.startswith("pr_branch:")])
    fix, rr = fix_and_re_review(rev)
    assert "pr_branch:feature/x" in json.loads(rr["labels"]), rr["labels"]
    assert f"publish #{fix['id']}'s head onto its pr_branch feature/x" in rr["spec"], rr["spec"]

    # Own pushes turned off since the first review: as the first review then would, the stack lands on
    # the push branch like any other change (no review-only label), and the re-review never mentions --own.
    _, rev = failed_stack("three")
    p.set_config("delivery.own_push", "off")
    fix, rr = fix_and_re_review(rev)
    labels = json.loads(rr["labels"])
    assert not any(lb.startswith("pr_branch:") for lb in labels) and push.REVIEW_ONLY_LABEL not in labels, labels
    assert "--own" not in rr["spec"] and "Review only" not in rr["spec"], rr["spec"]
    assert "Its branch label feature/x was ignored: own pushes are off" in rr["spec"], rr["spec"]
    assert f"push #{fix['id']}'s head with `ttp push` from #{fix['id']}'s worktree" in rr["spec"], rr["spec"]
    assert "do not push it" not in fix["spec"] and f"Leave the push to re-review #{rr['id']}" in fix["spec"]
    # ...unless nothing may reach the push branch: then review only, saying why.
    p.set_config("delivery.own_push", "on")
    _, rev = failed_stack("four")
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND title='four'"):
        p.db.update_task(t["id"], spec="no_push: an experiment")
    p.set_config("delivery.own_push", "off")
    _, rr = fix_and_re_review(rev)
    assert push.REVIEW_ONLY_LABEL in json.loads(rr["labels"]), rr["labels"]
    assert "--own" not in rr["spec"] and "Review only: own pushes are off" in rr["spec"], rr["spec"]
    assert "must not reach the push branch" in rr["spec"], rr["spec"]


def test_a_push_queue_stack_whose_spec_or_hand_off_mentions_own_keeps_its_push_step_on_re_review(env, monkeypatch):
    """A plain stack (no pr_branch) whose code spec and hand-off mention `ttp push --own`: the first
    review pushes with `ttp push`, and so does the re-review after it fails. The mention is the code
    task's text, not the review's push step: the re-review is not review only and keeps that text."""
    from ttp import push, pushq
    from ttp.daemon import Daemon
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    spec = ("Publish with `ttp push --own --detach` when asked.\n"
            "It delivers onto its pr_branch nowhere, not here. If it passes, publish it there with "
            "`ttp push --own --detach`.")
    cid, _, _, (rev,) = _finish_code(env, p, "plain", {"plain.py": 3},
                                     result={"status": "done", "summary": "fixed the `ttp push --own` refusal"},
                                     before=lambda tid, head: p.db.update_task(tid, spec=spec))
    assert "push it with `ttp push`" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    p.db.update_task(rev["id"], status="failed")
    fid, rid, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    rr, fix = p.db.task(rid), p.db.task(fid)
    labels = json.loads(rr["labels"])
    assert push.REVIEW_ONLY_LABEL not in labels and not any(lb.startswith("pr_branch:") for lb in labels), labels
    assert "Review only" not in rr["spec"] and "push it with `ttp push`" in rr["spec"], rr["spec"]
    assert "fixed the `ttp push --own` refusal" in rr["spec"] and "If it passes, publish it there with `ttp push --own --detach`." in rr["spec"], rr["spec"]
    assert f"Leave the push to re-review #{rid}" in fix["spec"]


def test_the_review_of_a_task_with_another_pr_branch_publishes_with_own_and_never_approves_for_the_queue(
        env, monkeypatch):
    """A code task whose pr_branch is the push branch gets the usual push step; one whose pr_branch is
    another branch gets a review that publishes with `ttp push --own` and is review only for the push
    branch: no plain `ttp push`, no push-queue approval."""
    from ttp import push, pushq
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)   # no prechecks: the spec text is the subject
    for on in (False, True):
        p.set_config("delivery.push_queue", on)
        _, _, _, (same,) = _finish_code(env, p, f"same {on}", {f"s{on}.py": 3}, labels=["pr_branch:work"])
        assert "ttp push` from" in same["spec"] and "--own" not in same["spec"], same["spec"]
        assert push.REVIEW_ONLY_LABEL not in json.loads(same["labels"])
        _, _, _, (other,) = _finish_code(env, p, f"other {on}", {f"o{on}.py": 3}, labels=["pr_branch:feature/x"])
        assert "ttp push` from" not in other["spec"], other["spec"]
        assert "pr_branch feature/x" in other["spec"] and "`ttp push --own --detach`" in other["spec"]
        assert "do not approve it for the push queue" in other["spec"]
        assert other["spec"].rstrip().endswith("the pushed commit.")
        assert push.REVIEW_ONLY_LABEL in json.loads(other["labels"])
    # A project that does not push: review only, no publish step.
    p.set_config("delivery.push_allowed", False)
    _, _, _, (rev,) = _finish_code(env, p, "no push", {"n.py": 3}, labels=["pr_branch:feature/x"])
    assert "Review only" in rev["spec"] and "--own --detach" not in rev["spec"], rev["spec"]
    coordinator = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "publish it with `ttp push --own --detach`\", never\n  plain `ttp push` or a push-queue approval" \
        in coordinator


def test_the_review_prompt_keeps_the_own_publish_step_when_the_push_queue_is_on(env, monkeypatch):
    """With the push queue on, a review whose spec says to publish with `ttp push --own` still gets
    the --own section with its --detach, retry_when and exit-code steps, and the queue section's
    'NEVER run `ttp push`' names it as the exception, so the two never contradict."""
    from ttp import pushq
    from ttp.prompts import worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    p.set_config("delivery.push_queue", True)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _, _, _, (rev,) = _finish_code(env, p, "other", {"o.py": 3}, labels=["pr_branch:feature/x"])
    text = worker_task(p, rev, str(p.root), None)
    assert "push queue=True" in text and "`ttp push --own --detach`" in rev["spec"]
    own = text[text.index("## Publishing onto its own PR branch (when the spec says `ttp push --own`)"):]
    own = own[:own.index("\n## ")]
    assert "whatever the sections below say, with or without the push queue" in own
    assert "run `ttp push --own --detach` in the\n  change's worktree" in own
    assert "never approve it for the push queue" in own and "`waiting` with that\n  `retry_when`" in own
    for code in ("0 pushed", "3: rebase conflict", "4: a check failed", "5: the\n  branch kept moving",
                 "75: another push", "2 or 6: refused"):
        assert code in own, code
    queue = text[text.index("## Approving into the push queue"):]
    assert "NEVER run `ttp push` or `git push`" in queue
    assert "The one\n  exception: a spec that says to publish with `ttp push --own`" in queue
    # Without the queue the section is there too, next to the plain push section.
    p.set_config("delivery.push_queue", False)
    off = worker_task(p, rev, str(p.root), None)
    assert "## Publishing onto its own PR branch" in off and "## Approving into the push queue" not in off


def test_the_review_of_a_task_whose_pr_branch_is_absent_from_the_remote_delivers_to_the_push_branch(
        env, monkeypatch):
    """With the push queue on, a pr_branch the push remote has no branch of is ignored, as the queue's
    check_approval ignores it: the review gets the usual push step and the label is named as ignored.
    A task whose spec asks for `ttp push --own` (it creates that branch) keeps the --own step only with
    delivery.own_push on, and a pr_branch the remote has keeps it too."""
    from ttp import push, pushq, settinghold
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.push_queue", True)
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: True)
    monkeypatch.setattr(Daemon, "_precheck", lambda *a: None)   # no checks: the spec text is the subject
    _, _, _, (gone,) = _finish_code(env, p, "gone", {"g.py": 3}, labels=["pr_branch:feature/x"])
    assert "ttp push` from" in gone["spec"] and "--own" not in gone["spec"], gone["spec"]
    assert "pr_branch label feature/x was ignored: origin has no such branch" in gone["spec"]
    assert push.REVIEW_ONLY_LABEL not in json.loads(gone["labels"])

    def own(tid, head):
        p.db.update_task(tid, spec="Create feature/x: publish it with `ttp push --own --detach`.")
    # delivery.own_push unset (existing_only) creates no branch, whatever the spec asks: the push queue
    # takes it. A reported case: reviews created such branches with --own in a one-branch project.
    _, _, _, (unset,) = _finish_code(env, p, "unset", {"u.py": 3}, labels=["pr_branch:feature/x"], before=own)
    assert "was ignored" in unset["spec"] and "publish it with" not in unset["spec"].split("Its hand-off")[1]
    assert "ttp push` from" in unset["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(unset["labels"])
    p.set_config("delivery.own_push", "on")
    _, _, _, (mine,) = _finish_code(env, p, "own", {"m.py": 3}, labels=["pr_branch:feature/x"], before=own)
    assert "`ttp push --own --detach`" in mine["spec"] and "was ignored" not in mine["spec"], mine["spec"]
    assert push.REVIEW_ONLY_LABEL in json.loads(mine["labels"])
    # A pr_branch the remote has: its PR's branch, --own as before.
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    _, _, _, (there,) = _finish_code(env, p, "there", {"t.py": 3}, labels=["pr_branch:feature/x"])
    assert "`ttp push --own --detach`" in there["spec"] and "was ignored" not in there["spec"]
    # asks_own reads an --own instruction, never a negated one or a plain push.
    assert settinghold.asks_own({"spec": "- ttp push --own --detach"})
    assert settinghold.asks_own({"title": "publish it with `ttp push --own`"})
    for spec in ("Do not use `ttp push --own` here.", "push it with `ttp push --detach`", "mentions --own"):
        assert not settinghold.asks_own({"spec": spec}), spec


def test_pr_elsewhere_compares_the_push_branchs_branch_part(env):
    """push_branch `team/x` is branch team/x of origin (team is no remote): pr_branch x is another
    branch; `origin/x` and `refs/heads/x` are x."""
    from ttp import push
    p = make(env)
    _with_origin(env, clone=False)
    t = {"id": 1, "labels": json.dumps(["pr_branch:x"])}
    assert push.pr_elsewhere(p, t, {"push_branch": "team/x"}) == "x"
    for same in ("origin/x", "x", "refs/heads/x", "origin/refs/heads/x"):
        assert push.pr_elsewhere(p, t, {"push_branch": same}) == "", same
    assert push.pr_elsewhere(p, {"id": 2, "labels": json.dumps(["pr_branch:team/x"])},
                             {"push_branch": "team/x"}) == ""


def test_the_push_queue_refuses_an_approval_of_a_task_with_another_pr_branch(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    entries = [{"branch": s.branch, "head": s.head}]
    s.p.db.update_task(s.code, labels=["pr_branch:proj"])   # the push branch itself: approved as usual
    assert pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("entries")
    _git_out(s.p.root, "push", "-q", "origin", "HEAD:refs/heads/feature/x")   # on the remote: its PR's branch
    s.p.db.update_task(s.code, labels=["pr_branch:feature/x"])
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert "pr_branch feature/x" in check.get("invalid", "") and "ttp push --own" in check["invalid"], check
    # The review's run approving it anyway runs again once, told why, and nothing enters the queue.
    task = _pq_hand_off(env, s)
    assert task["status"] == "queued" and not s.p.db.q("SELECT * FROM push_queue")


def test_the_push_queue_accepts_an_approval_whose_pr_branch_label_names_a_branch_absent_from_the_remote(
        env, monkeypatch):
    """A pr_branch label inherited through `continues` from a failed review may name a branch that
    exists only locally: no PR to deliver onto, so the approval is a push-branch delivery and the
    feed says the label was ignored. A remote that cannot be reached still refuses, as before."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _git_out(s.p.root, "branch", "local-only", s.head)
    s.p.db.update_task(s.code, labels=["pr_branch:local-only"])
    entries = [{"branch": s.branch, "head": s.head}]
    check = pushq.check_approval(s.p, s.p.db.task(s.review), entries)
    assert check.get("entries") and check["target"] == "origin/proj", check
    [note] = check["notes"]
    assert f"#{s.code}'s pr_branch label local-only was ignored" in note and "no such branch" in note
    assert note in pushq.queued_text(s.p.db.task(s.review), check)
    # Unreachable remote: no answer, so refused as before.
    absent = pushq._absent
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    assert "ttp push --own" in pushq.check_approval(s.p, s.p.db.task(s.review), entries).get("invalid", "")
    monkeypatch.setattr(pushq, "_absent", absent)
    task = _pq_hand_off(env, s)
    assert task["status"] == "pushing", task
    [row] = s.p.db.q("SELECT * FROM push_queue")
    assert row["head"] == s.head and row["target"] == "origin/proj"
    [ev] = s.p.db.q("SELECT text FROM events WHERE kind='push_queued'")
    assert "local-only was ignored" in ev["text"]


def test_a_plain_push_refuses_a_head_whose_task_has_another_pr_branch_and_own_publishes_onto_it(
        env, monkeypatch):
    """Plain `ttp push` of a task that delivers onto a pr_branch other than the push branch is refused,
    naming --own, in the task's run, its review's and its review fix's; `ttp push --own` from the
    review publishes onto that pr_branch. A pr_branch that is the push branch pushes as before."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")

    def task(labels, kind="code", **more):
        tid = p.db.add_task(f"t{len(p.db.q('SELECT id FROM tasks'))}", "s", kind=kind, labels=labels, **more)
        return tid, f"ttp/t{tid}-x"

    code, branch = task(["pr_branch:feature/x"])
    _git_out(repo, "checkout", "-q", "-B", branch)
    for runner in (str(code), str(task([f"auto_review:{code}"], kind="review", depends_on=[code])[0])):
        monkeypatch.setenv("TTP_TASK", runner)
        with pytest.raises(ValueError, match=r"pr_branch feature/x \(its own label\), not origin/proj.*"
                                             r"ttp push --own --detach"):
            push.resolve(p, repo)
        assert push.resolve(p, repo, own=True) == ("origin", "feature/x", True), runner
    # The review's run in the PR branch's own worktree (the task worked on it directly).
    _git_out(repo, "checkout", "-q", "-B", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    # A fix of a failed review continues the task: its branch delivers onto the same PR.
    fix, fbranch = task([f"continues:{code}"])
    _git_out(repo, "checkout", "-q", "-B", fbranch)
    monkeypatch.setenv("TTP_TASK", str(fix))
    # It names the task whose label it inherits and how the coordinator clears it.
    with pytest.raises(ValueError, match=rf"pr_branch feature/x \(the label of #{code}, which it continues\).*"
                                         rf"task_update pr_branch \"\".*task_add with continues #{fix} and "
                                         rf"pr_branch \"\""):
        push.resolve(p, repo)
    # A branch on the remote or here is published; one in neither place never was.
    d = p.config()["delivery"]
    assert not push.never_published(repo, d, "feature/x") and not push.never_published(repo, d, fbranch)
    assert push.never_published(repo, d, "feature/never-made")
    # A remote that cannot be read cannot tell.
    _git_out(repo, "remote", "add", "dead", str(env["tmp"] / "no-such-remote.git"))
    assert not push.never_published(repo, {"push_branch": "dead/proj"}, "feature/never-made")
    _git_out(repo, "remote", "remove", "dead")
    # pr_branch is the push branch: the usual target.
    same, sbranch = task(["pr_branch:proj"])
    _git_out(repo, "checkout", "-q", "-B", sbranch)
    monkeypatch.setenv("TTP_TASK", str(same))
    _record_checks_pass(p, same, repo)   # a code task lands only tested work
    assert push.resolve(p, repo) == ("origin", "proj", False)
    monkeypatch.delenv("TTP_TASK")
    assert push.resolve(p, repo) == ("origin", "proj", False)


def test_a_notes_branch_publishes_itself_and_never_rebases_onto_the_push_branch_or_a_pr_branch(
        env, monkeypatch):
    """A task's notes branch (ttp/t<id>-notes) is pushed under its own name as it is, whether or not
    the task carries a pr_branch label: a plain push never rebases it onto the push branch (it
    conflicted there) and --own never publishes it onto the pr_branch. The task's code branch and
    the pr_branch's own worktree still deliver onto the pr_branch."""
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    base = _git_out(repo, "rev-parse", "HEAD")
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    proj, feature = _git_out(origin, "rev-parse", "proj"), _git_out(origin, "rev-parse", "feature/x")
    for labels in ([], ["pr_branch:feature/x"]):
        tid = p.db.add_task(f"lessons {labels}", "s", kind="code", labels=labels)
        notes = f"ttp/t{tid}-notes"
        _git_out(repo, "checkout", "-q", "-B", notes, base)
        _commit(repo, f"notes{tid}.md", "notes\n")
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert push.resolve(p, repo) == ("origin", notes, True), labels
        assert push.own_target(p, repo) == ("origin", notes), labels
        head = _git_out(repo, "rev-parse", "HEAD")
        assert _ttp_push() == 0, labels
        assert _git_out(origin, "rev-parse", notes) == head, labels   # as it is: not rebased
        assert _git_out(origin, "rev-parse", "proj") == proj
        assert _git_out(origin, "rev-parse", "feature/x") == feature
    # The same task's code branch, and the pr_branch's own worktree, still go to the pr_branch.
    _git_out(repo, "checkout", "-q", "-B", f"ttp/t{tid}-code")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    _git_out(repo, "checkout", "-q", "-B", "feature/x")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    with pytest.raises(ValueError, match="ttp push --own"):
        push.resolve(p, repo)
    # Only a whole `notes` word ends a notes branch.
    assert not push.NOTES_BRANCH.fullmatch(f"ttp/t{tid}-keynotes")
    assert push.NOTES_BRANCH.fullmatch(f"ttp/t{tid}-swirl-notes")


def test_a_code_task_whose_title_ends_in_notes_still_delivers_its_own_branch_as_code(env, monkeypatch):
    """A code task titled 'Update release notes' works on ttp/t<id>-update-release-notes, which
    matches NOTES_BRANCH by name: it is still the task's own code branch, so a plain push targets
    the push branch and own_target carries it onto the task's pr_branch."""
    from ttp import push, worktree
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(other, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    for labels in ([], ["pr_branch:feature/x"]):
        tid = p.db.add_task("Update release notes", "s", kind="code", labels=labels)
        own = f"ttp/t{tid}-{worktree.slug('Update release notes')}"
        assert push.NOTES_BRANCH.fullmatch(own)
        _git_out(repo, "checkout", "-q", "-B", own)
        _commit(repo, f"rel{tid}.md", "release\n")
        monkeypatch.setenv("TTP_TASK", str(tid))
        assert not push.is_notes_branch(p, own)
        if labels:
            assert push.own_target(p, repo) == ("origin", "feature/x")
        else:   # its spec has no landing step: `--land` says it may land
            monkeypatch.setenv(push.LAND_ENV, "1")
            assert push.resolve(p, repo) == ("origin", "proj", False)
            monkeypatch.delenv(push.LAND_ENV)
    # A branch recorded on the task row is its own code branch too; the slug then no longer is.
    p.db.x("UPDATE tasks SET branch=? WHERE id=?", (f"ttp/t{tid}-fix-notes", tid))
    assert not push.is_notes_branch(p, f"ttp/t{tid}-fix-notes")
    assert push.is_notes_branch(p, own) and push.is_notes_branch(p, f"ttp/t{tid}-notes")


def test_batch_inherited_takes_only_the_named_fd(env, tmp_path, monkeypatch):
    """batch._inherited trusts only the descriptor in TTP_BATCH_LOCK_FD: a caller's own descriptor on
    the same lock file (batch run in-process) is never picked up or closed."""
    from ttp import batch
    path = tmp_path / "push:run-b1.0.lock"
    path.write_text("")
    mine = os.open(path, os.O_RDWR)
    try:
        monkeypatch.delenv(batch.LOCK_FD_ENV, raising=False)
        assert batch._inherited(path) is None
        other = tmp_path / "other.lock"
        other.write_text("")
        passed = os.open(other, os.O_RDWR)
        monkeypatch.setenv(batch.LOCK_FD_ENV, str(passed))
        assert batch._inherited(path) is None        # named fd is on another file
        assert batch._inherited(other) == passed
        os.close(passed)
        assert batch._inherited(other) is None       # named fd no longer open
        monkeypatch.setenv(batch.LOCK_FD_ENV, str(mine))
        assert batch._inherited(path) == mine
        os.fstat(mine)                               # still open
    finally:
        os.close(mine)


def test_ttp_checks_drops_an_extra_command_the_project_checks_already_run(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count = tmp_path / "count"
    check = f"echo x >> {count}"
    p.set_config("delivery.push_checks", [check])
    cli.main(["checks", "--fresh", "--", check, "test -f a.txt"])
    out = capsys.readouterr().out
    assert len(count.read_text().splitlines()) == 1, "the push check ran once"
    assert json.loads((run / "checks.json").read_text())["commands"] == [check, "test -f a.txt"]
    assert out.count("delivery.push_checks already runs it") == 1 and repr(check) in out
    # The same command in another form, or given twice, is still one command.
    own = push.check_list(["pytest -q tests/a.py"])
    assert cli._extra_checks(["pytest", "-q", "tests/a.py"], own) == ([], ["pytest -q tests/a.py"])
    assert cli._extra_checks(["pytest  -q 'tests/a.py'"], own) == ([], ["pytest  -q 'tests/a.py'"])
    assert cli._extra_checks(["ruff check .", "ruff  check ."], own) == (["ruff check ."], ["ruff  check ."])
    # A scoped project check may be skipped on this head: the extra that matches it stays.
    scoped = push.check_list([{"run": "pytest -q", "if_changed": "src/**"}])
    assert cli._extra_checks(["pytest", "-q"], scoped) == (["pytest -q"], [])


def _recording_venv(path, ran):
    """A venv stand-in whose python records how it was called, with a pytest on its PATH."""
    venv = _fake_venv(path)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\necho "$@" >> {ran}\n')
    (venv / "bin" / "pytest").write_text(f"#!/bin/sh\necho venv-pytest >> {ran}\n")
    for name in ("python", "pytest"):
        (venv / "bin" / name).chmod(0o755)
    return venv


def test_ttp_checks_runs_an_extra_pytest_once_and_in_the_projects_venv(env, tmp_path, monkeypatch, capsys):
    """Replays `ttp checks -- 'pytest -q --no-device-tests'` beside a project check that already runs
    it: it ran twice, the second time with the pytest on PATH, whose python lacked the project's
    packages, so it failed at import after the first run passed."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    ran = tmp_path / "ran"
    venv = _recording_venv(p.root / ".venv", ran)
    stub = tmp_path / "stub"   # the pytest PATH finds outside the venv: no project packages
    stub.mkdir()
    (stub / "pytest").write_text(f"#!/bin/sh\necho path-pytest >> {ran}\nexit 1\n")
    (stub / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")
    cmd = "pytest -q --no-device-tests"
    # A scoped project check that applies on this head runs it: the extra is dropped, not run again.
    p.set_config("delivery.push_checks", [{"run": cmd, "if_exists": "a.txt"}])
    cli.main(["checks", "--fresh", "--", cmd])
    out = capsys.readouterr().out
    runs = lambda: [ln for ln in ran.read_text().splitlines() if ln != "-c pass"]   # not the venv probe
    assert runs() == ["-m pytest -q --no-device-tests"], "once, with the project venv's python"
    assert "a scoped delivery.push_checks entry runs it on this head" in out
    # Another spelling of the same pytest run is the same command.
    ran.unlink()
    p.set_config("delivery.push_checks", [f"{venv}/bin/python -m pytest -q --no-device-tests"])
    cli.main(["checks", "--fresh", "--", cmd])
    assert len(runs()) == 1
    assert "delivery.push_checks already runs it" in capsys.readouterr().out
    # An extra pytest no project check runs still uses the project's venv, not the pytest on PATH.
    ran.unlink()
    p.set_config("delivery.push_checks", ["true"])
    cli.main(["checks", "--fresh", "--", "pytest -q tests/x.py"])
    assert runs() == ["-m pytest -q tests/x.py"]
    # The same with the venv already active (a run's environment): VIRTUAL_ENV picks it.
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    assert push.venv_cmd("pytest -q", repo) == f"{shlex.quote(str(venv / 'bin' / 'python'))} -m pytest -q"
    assert push.venv_cmd("pytest -q", repo, {}) == "pytest -q"
    # Spellings of one pytest run; shell syntax stays as written.
    keys = {cli._same_run_key(c, "/x/.venv/bin/python") for c in (
        "pytest -q", "py.test -q", "python -m pytest -q", "python3 -m pytest -q", "/x/.venv/bin/python -m pytest -q")}
    assert keys == {"pytest -q"}
    # Only those: another explicit interpreter runs as written, so it is not the same run.
    for c in ("/usr/bin/python3 -m pytest -q", "python3.11 -m pytest -q", ".venv/bin/pytest -q",
              "/x/.venv/bin/python -m pytest -q"):
        assert cli._same_run_key(c) == c
    assert cli._extra_checks(["/usr/bin/python3 -m pytest -q"], push.check_list(["pytest -q"])) == (
        ["/usr/bin/python3 -m pytest -q"], [])
    assert cli._extra_checks(["/x/.venv/bin/python -m pytest -q"], push.check_list(["pytest -q"]),
                             "/x/.venv/bin/python") == ([], ["/x/.venv/bin/python -m pytest -q"])
    assert cli._same_run_key("pytest -q; true") == "pytest -q; true"
    assert cli._same_run_key("python3 tools/pytest.py") == "python3 tools/pytest.py"


def test_ttp_checks_warns_of_a_broken_venv_with_the_command_that_makes_it_again(env, tmp_path, monkeypatch, capsys):
    """A project-root .venv link to a venv made on another OS (its python points at an interpreter
    missing here) leaves the checks without its packages: ttp checks says so and how to rebuild it."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (repo / ".gitignore").write_text(".venv\nvenv/\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n[project.optional-dependencies]\ntest = ["pytest"]\n')
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "b"], check=True)
    real = _fake_venv(repo / "venv", python=False)
    (real / "bin" / "python").symlink_to(tmp_path / "elsewhere" / "python3")
    (repo / ".venv").symlink_to("venv")
    p.set_config("delivery.push_checks", ["true"])
    cli.main(["checks", "--fresh"])
    out = capsys.readouterr().out
    assert out.count("warning: the venv") == 1, out   # .venv and venv are one venv
    assert f"python3 -m venv --clear {real} && {real}/bin/python -m pip install -e '{repo}[test]'" in out
    assert "warning: the venv" in (run / "checks.log").read_text()
    assert push.venv_cmd("pytest -q", repo) == "pytest -q"   # never the broken venv's python
    # A link that dangles, and a python that does not run; a working venv is not named.
    (repo / ".venv").unlink()
    (repo / ".venv").symlink_to(tmp_path / "gone")
    (real / "bin" / "python").unlink()
    (real / "bin" / "python").write_text("#!/bin/sh\necho bad interpreter >&2\nexit 1\n")
    (real / "bin" / "python").chmod(0o755)
    found = dict(push.broken_venvs(repo))
    assert "which is missing" in found[repo / ".venv"] and "bad interpreter" in found[repo / "venv"]
    (real / "bin" / "python").unlink()
    (real / "bin" / "python").symlink_to(sys.executable)
    assert list(push.broken_venvs(repo)) == [(repo / ".venv", found[repo / ".venv"])]
    assert push.venv_rebuild_cmd(tmp_path / "plain" / ".venv").endswith("-m pip install pytest")


def test_ttp_checks_still_reads_a_checks_record_from_before_extras_were_split(env, tmp_path, monkeypatch):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (run / "checks.json").write_text(json.dumps({"head": head, "worktree": str(repo), "passed": True,
                                                 "commands": ["pytest -q", "test -n 'a b'"], "skipped": [],
                                                 "ts": time.time()}))
    assert push.recorded_extras(push.recorded_checks(run), head, push.check_list(["pytest -q"])) == ["test -n 'a b'"]
    cli.main(["checks", "--", "test", "-n", "a b"])
    assert json.loads((run / "checks.json").read_text())["commands"] == ["test -n 'a b'"]


@pytest.mark.parametrize("box", ["pidns", "cgroup"])
def test_detach_from_a_sandboxed_command_is_queued_and_the_daemon_starts_it(env, box):
    """A command in a PID namespace or cgroup other than its run's may take everything it started
    along when it ends: the job is queued, reads as waiting, and the daemon starts it."""
    from ttp import detach, locks, push
    if not (push.pid_ns() if box == "pidns" else push.cgroup()):
        pytest.skip("no /proc")
    p = _device_project(env, device=False)
    run_dir = p.runs / "8"
    run_dir.mkdir(parents=True)
    other = {"TTP_PIDNS": "pid:[1]"} if box == "pidns" else {"TTP_CGROUP": "0::/elsewhere"}
    r = _ttp_run(p, "detach", "job1", "--", "sh", "-c", 'echo "hi $JOB_VAR"; exit 3',
                 env={"TTP_RUN_DIR": str(run_dir), "JOB_VAR": "there", "SOME_API_TOKEN": "s3cret", **other})
    assert r.returncode == 0 and "queued" in r.stdout, r
    rc = run_dir / "job1.rc"
    queue = list((p.state / detach.QUEUE).glob("*.json"))
    assert len(queue) == 1 and "s3cret" not in queue[0].read_text(), "secrets stay off disk"
    assert json.loads((run_dir / "detached.json").read_text())[0]["pid"] is None
    check = _ttp_run(p, "detach", "--check", str(rc))
    assert check.returncode == 1 and "queued for the daemon" in check.stdout, check
    assert not locks.job_ended(rc), "an exclusive task's resources stay held while its job waits"
    detach.tend(p)
    assert not list((p.state / detach.QUEUE).glob("*.json")) and not detach.queued(rc).exists()
    check = _check_until_ended(p, rc)
    assert check.returncode == 4 and "rc=3" in check.stdout, check
    assert (run_dir / "job1.log").read_text().strip() == "hi there"


def test_detach_inside_a_pid_namespace_that_ends_with_the_command_still_runs_the_job(env, tmp_path):
    """The reported failure, reproduced: a namespace that ends with the command kills what it started,
    so a job started in it never wrote a line. Queued, the daemon runs it to its exit code."""
    from ttp import detach, push
    unshare = shutil.which("unshare")
    if not (unshare and push.pid_ns()):
        pytest.skip("no unshare or /proc")
    box = [unshare, "--user", "--pid", "--fork", "--map-root-user", "--mount-proc"]
    if subprocess.run([*box, "true"], capture_output=True).returncode:
        pytest.skip("unprivileged PID namespaces are not allowed here")
    p = _device_project(env, device=False)
    run_dir = p.runs / "8"
    run_dir.mkdir(parents=True)
    e = {**os.environ, "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(run_dir), "TTP_PIDNS": push.pid_ns()}
    # bare: as before, no run namespace to compare. Its job outlives a slow exit of `ttp detach` under
    # load, so only the namespace's end stops it.
    for name, pidns, nap in (("bare", "", 300), ("job", push.pid_ns(), 1)):
        r = subprocess.run([*box, sys.executable, str(TTP), "detach", name, "--", "sh", "-c", f"sleep {nap}; echo done; exit 5"],
                           env={**e, "TTP_PIDNS": pidns}, cwd=str(p.root), capture_output=True, text=True, timeout=240)   # ttp startup alone took >30 s at host load 187
        assert r.returncode == 0, r
    bare = _check_until_ended(p, run_dir / "bare.rc")
    assert "gone without an exit code (it " in bare.stdout and (run_dir / "bare.lost").exists(), bare
    assert _ttp_run(p, "detach", "--check", str(run_dir / "job.rc")).returncode == 1
    detach.tend(p)
    check = _check_until_ended(p, run_dir / "job.rc")
    assert check.returncode == 4 and "rc=5" in check.stdout, check
    assert (run_dir / "job.log").read_text().strip() == "done"


def test_macos_boot_id_prefers_bootsessionuuid_and_still_knows_the_boottime_seconds(env, tmp_path, monkeypatch):
    """kern.boottime moves when the clock is stepped, so the boot id is kern.bootsessionuuid where the host
    has it. A boot recorded by an older version as kern.boottime's seconds still reads as this boot, in the
    runner and the daemon, so an upgrade is never taken for a reboot."""
    from ttp import push, runner
    from ttp.daemon import Daemon
    p = make(env)
    fake, booted, uuid = tmp_path / "fakebin", tmp_path / "booted", tmp_path / "uuid"
    fake.mkdir()
    booted.write_text("1700000000")
    _fake_sysctl(fake, booted, uuid)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(runner, "BOOT_ID_FILE", tmp_path / "noproc" / "boot_id")
    # No uuid (an older macOS): kern.boottime's seconds, as before.
    assert runner.boot_id() == "1700000000" and runner.boot_ids() == ("1700000000", "1700000000")
    uuid.write_text("6A1C0E4B-0000-4000-8000-000000000001\n")
    assert runner.boot_id() == "6A1C0E4B-0000-4000-8000-000000000001"
    assert runner.boot_ids() == ("6A1C0E4B-0000-4000-8000-000000000001", "1700000000")
    assert runner.same_boot("1700000000") and runner.same_boot("6A1C0E4B-0000-4000-8000-000000000001")
    assert not runner.same_boot("1699999999") and not runner.same_boot("") and not runner.same_boot(None)
    assert "rebooted" not in push._dead_reason({"boot": "1700000000"})
    assert "rebooted" in push._dead_reason({"boot": "1699999999"})
    # A stepped clock moves kern.boottime but not the boot id.
    booted.write_text("1700000042")
    assert runner.boot_id() == "6A1C0E4B-0000-4000-8000-000000000001"
    # The daemon after an upgrade: a run, a pause until reboot and the last told boot recorded with the
    # seconds are this boot's; a run of an earlier boot is not.
    d = Daemon(p.base)
    assert d.boots == ("6A1C0E4B-0000-4000-8000-000000000001", "1700000042")
    assert d._this_boot("1700000042") and not d._this_boot("1700000000") and not d._this_boot(None)
    p.db.set_kv("paused", True)
    p.db.set_kv("pause_until_boot", {"boot": "1700000042", "since": time.time()})
    Daemon(p.base).tick()
    assert p.db.kv("paused"), "a pause until reboot recorded with the seconds is not lifted by an upgrade"


def test_daily_review_target_line_reads_the_last_pushed_batch_reach(env, tmp_path):
    from ttp import pushq
    from ttp.db import DB
    db = DB(tmp_path / "t.db")
    assert pushq.target_line(db) is None   # nothing pushed yet

    def batch(i, reach, marker=True, started=1.0):
        m = tmp_path / f"{i}.json"
        if marker:
            m.write_text(json.dumps({"reach": reach} if reach is not None else {}))
        db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
             "VALUES(?,?,?,?,?,?,'pushed','ab12345')", (i, str(m), "origin/proj", started, started, started))

    batch("none", None)   # same branch: push.reach recorded nothing
    assert pushq.target_line(db) is None
    batch("gone", {"ref": "origin/main", "on": False, "behind": 2}, marker=False, started=2.0)
    assert pushq.target_line(db) is None
    batch("behind", {"ref": "origin/main", "on": False, "behind": 4}, started=3.0)
    assert pushq.target_line(db) == ("Intended target origin/main is 4 commits behind origin/proj "
                                     "(as of the last push, 1970-01-01 00:00Z)")
    batch("one", {"ref": "origin/main", "on": False, "behind": 1}, started=4.0)
    assert "is 1 commit behind" in pushq.target_line(db)
    batch("on", {"ref": "origin/main", "on": True, "behind": 0}, started=5.0)
    assert "origin/main is up to date with origin/proj" in pushq.target_line(db)
    batch("unread", {"ref": "origin/main", "on": None, "behind": None}, started=6.0)
    assert pushq.target_line(db).startswith("Intended target origin/main could not be read at the last push")
    batch("same", {"ref": "origin/proj", "on": True, "behind": 0}, started=7.0)
    assert pushq.target_line(db) is None


def test_landed_falls_back_to_the_local_branch_in_a_repo_with_no_remote(tmp_path, monkeypatch):
    """A remote-less repo: the push target's remote does not exist, so look on refs/heads/<branch>."""
    from ttp import landed, push
    repo = tmp_path / "r"
    repo.mkdir()
    _git_out(repo, "init", "-q", "-b", "main")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "a")
    sha = _git_out(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(push, "target", lambda p, r: ("origin", "main"))
    assert landed._push_ref(None, repo) == "refs/heads/main"
    assert landed.check(None, repo, sha=sha)[0] == 0
    _git_out(repo, "checkout", "-q", "-b", "side")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "b")
    assert landed.check(None, repo, sha=_git_out(repo, "rev-parse", "HEAD"))[0] == 1


def test_run_notes_memory_ends_sweep_spend_and_deploy_times_use_the_home_zone_on_a_utc_box(env, utc_box, monkeypatch):
    """On a UTC box, a Los Angeles project's user-facing times carry PDT/PST, never UTC or 'Z': the run
    notes people read live (ttp note, notify, push and lock lines), a temporary memory's end in the
    prompts, the blocker sweep's deferred reason (digest and daily review), the account spend window
    and the deploy-behind alert after a push."""
    from datetime import datetime, timezone as dt_timezone
    from types import SimpleNamespace
    from ttp import cli, deploy, ends, push, sweep, timefmt
    from ttp import coordinator as coord
    pac = re.compile(r"\d\d:\d\d(:\d\d)? P[DS]T")
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=dt_timezone.utc).timestamp()
    # The formatter for live lines: the project in the environment, else UTC.
    assert timefmt.clock({"TTP_PROJECT": str(p.base)}, now=summer) == "05:00:00 PDT"
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    assert timefmt.clock({}, now=summer) == "12:00:00 UTC"
    # Run notes: ttp note, ttp notify and a push's own note.
    run = env["home"] / "run-tz"
    run.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    cli.cmd_note(SimpleNamespace(text="milestone", to=None))
    cli.cmd_notify(SimpleNamespace(text="please review", severity="low"))
    push.run_note("pushed")
    notes = (run / "progress.md").read_text().splitlines()
    assert len(notes) == 3 and all(re.match(r"\d\d:\d\d:\d\d P[DS]T ", ln) for ln in notes), notes
    # A temporary memory's end, as the prompts show it; the file keeps its UTC stamp.
    end = {"expires": summer}
    assert ends.describe(end) == "ends 2026-07-01T12:00Z"
    assert ends.describe(end, p) == "ends 2026-07-01 05:00 PDT"
    (p.memory_dir / "fact-tz.md").write_text("---\nkind: fact\n" + ends.front_matter(end) + "---\nA box is lent out.\n")
    [line] = [e["line"] for e in p._memory_entries() if e["name"] == "fact-tz"]
    assert line.endswith("(ends 2026-07-01 05:00 PDT)") and "Z)" not in line, line
    # The blocker sweep's deferred reason, in the digest's settings and the daily review's project.
    now = time.time()
    tid = p.db.add_task("later", "spec", kind="work", not_before=now + 3600)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(coord.defer_labels(now + 3600, None)), tid))
    for where in (p, p.config()):
        [item] = [i for i in sweep.items(p.db, now, where) if i["task"] == tid]
        assert item["reason"].startswith("starts after ") and pac.search(item["reason"]), item
        assert " UTC" not in item["reason"]
    assert "sweep.review_lines(db, where=self.p)" in (RUNTIME / "ttp" / "daemon.py").read_text()
    # A coordinator update in a run's steer.md (which workers quote) is stamped in the home zone.
    rd = p.runs / "9999"
    rd.mkdir(parents=True)
    coord._append_update(rd / "steer.md", "use the other board", "1.0")
    assert re.search(r"^## Update \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T \(turn 1\.0\)$",
                     (rd / "steer.md").read_text(), re.M), (rd / "steer.md").read_text()
    # The deploy-behind alert an after_push's verdict raises.
    sent = []
    deploy._alert(p, {"name": "demo", "target": "9.9.9", "since": summer}, "behind", "files behind", summer + 7200,
                  alert=lambda key, text, **k: sent.append(text))
    assert "behind since 2026-07-01 05:00 PDT" in sent[0] and " UTC" not in sent[0], sent
    # The account spend window: the budget's zone, never the box's.
    monkeypatch.setattr(cli, "load_account_settings", lambda: {"budget": {"timezone": "America/Los_Angeles"}})
    import ttp.globalcap as gcap
    monkeypatch.setattr(gcap, "answer", lambda *a, **k: {"host": "h", "projects": [], "rows": [], "errors": []})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_spend_today(SimpleNamespace(receive=False, since=summer, until=None, json=False))
    assert out.getvalue().rstrip().endswith("since 2026-07-01 05:00 PDT"), out.getvalue()


def test_push_queue_backoff_shows_the_home_zone_on_a_utc_box(env, monkeypatch, utc_box):
    from ttp import pushq
    s = _pq(env, monkeypatch)
    _pq_review(s, priority=1)
    s.p.set_config("home_timezone", "America/Los_Angeles")
    now = time.time()
    s.p.db.set_kv(pushq.KV, {"backoff_until": now + 600})
    rows, why = pushq.due(s.p, now)
    assert rows == [] and re.fullmatch(r"backing off until \d\d:\d\d P[DS]T", why), why


def test_digest_daily_review_and_prompt_headers_carry_the_home_zone(env):
    from ttp import coordinator as coord
    from ttp import prompts
    from ttp import pushq
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    d = coord.digest(p, {}, [], [])
    assert re.search(r"^# STATE at \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T \(the project's home zone, America/Los_Angeles\)",
                     d, re.M), d[:200]
    tid = p.db.add_task("t", "spec", kind="work")
    text = prompts.worker_task(p, p.db.task(tid), str(p.root), None)
    assert re.search(r"home time zone: America/Los_Angeles, now \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T: write times "
                     r"for the user in it", text)
    tpl = RUNTIME.parent / "template" / "prompts"
    assert "home zone" in (tpl / "coordinator.md").read_text() and "home zone" in (tpl / "worker.md").read_text()
    assert "(HH:MM in the home zone)" in (tpl / "coordinator.md").read_text()
    # The daily review's delivery line: the home zone, not UTC.
    m = p.root / "reach.json"
    m.write_text(json.dumps({"reach": {"ref": "origin/main", "on": False, "behind": 2}}))
    p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
           "VALUES('b',?,'origin/proj',?,?,?,'pushed','ab12345')", (str(m),) + (_utc(2026, 7, 1, 16, 0),) * 3)
    assert pushq.target_line(p.db, "America/Los_Angeles").endswith("(as of the last push, 2026-07-01 09:00 PDT)")
    assert "pushq.target_line(db, timefmt.home(self.p))" in (RUNTIME / "ttp" / "daemon.py").read_text()


def test_push_target_line_is_in_the_home_zone_on_a_utc_box(utc_box, tmp_path):
    from ttp import pushq
    from ttp.db import DB
    db = DB(tmp_path / "t.db")
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"reach": {"ref": "origin/main", "on": False, "behind": 2}}))
    db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,outcome,pushed_sha) "
         "VALUES('b',?,'origin/proj',1.0,1.0,1.0,'pushed','ab12345')", (str(m),))
    assert pushq.target_line(db, "America/Los_Angeles").endswith("(as of the last push, 1969-12-31 16:00 PST)")


def test_move_local_rechecks_the_worktree_after_the_checks(env, monkeypatch):
    """The checks take time: a worktree that took uncommitted changes meanwhile is refused at the
    move itself, and the branch keeps its tip; once clean, the move goes through there."""
    from ttp import push
    p, repo = _local_push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "branch", "feature/x", "proj")
    side = env["tmp"] / "side"
    _git_out(repo, "worktree", "add", "-q", str(side), "feature/x")
    _git_out(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "a.txt", "a\n")
    head, before = _git_out(repo, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "feature/x")
    said = []
    (side / "x.txt").write_text("staged\n")
    _git_out(side, "add", "x.txt")
    assert push._move_local(repo, "feature/x", head, said.append) == push.REJECTED
    assert "uncommitted changes" in said[-1] and _git_out(repo, "rev-parse", "feature/x") == before
    _git_out(side, "reset", "-q", "--hard")
    assert push._move_local(repo, "feature/x", head, said.append) == 0
    assert _git_out(side, "rev-parse", "HEAD") == head


def test_push_own_with_a_git_remote_still_pushes_to_it_and_never_falls_back_to_the_local_branch(
        env, monkeypatch, capsys):
    """A repo with a remote is unchanged: an unreachable remote refuses (exit 2), never moving a local
    branch instead."""
    from ttp import push
    p, repo = _local_push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "remote", "add", "origin", str(env["tmp"] / "gone.git"))
    p.set_config("delivery.own_push", "on")   # unset (existing_only) would refuse before reaching the remote
    t = p.db.add_task("Speed it up", "s", kind="code", labels=["pr_branch:feature/x"])
    monkeypatch.setenv("TTP_TASK", str(t))
    _git_out(repo, "branch", "feature/x")
    _git_out(repo, "checkout", "-q", "-b", f"ttp/t{t}-speed-it-up")
    _commit(repo, "a.txt", "a\n")
    assert push.own_target(p, repo) == ("origin", "feature/x")
    before = _git_out(repo, "rev-parse", "feature/x")
    capsys.readouterr()
    assert _ttp("push", "--own") == 2 and "cannot reach origin" in capsys.readouterr().err
    assert _git_out(repo, "rev-parse", "feature/x") == before


def test_a_rebase_task_delivers_on_its_own_branch_and_its_review_publishes_that_one(env, monkeypatch):
    """A task whose title says it rebases the branch its spec names is labelled rebases:<branch>, not
    pr_branch: it starts from that branch's head on its own branch, its prompt says it delivers there,
    and its review publishes that new branch with --own (never the old one, never the queue)."""
    from ttp import coordinator as coord, push, pushq, worktree
    from ttp.prompts import worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "on")   # creating its branch takes own pushes set on
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(p.root, "branch", "feature/x")
    assert coord.apply(p, [{"type": "task_add", "title": "Rebase feature/x onto work", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nRebase it onto the latest work."}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='Rebase feature/x onto work'")
    assert json.loads(t["labels"]) == ["rebases:feature/x"]
    assert any("rebases feature/x" in n for n in p.db.kv(coord.NOTES_KEY) or [])
    assert worktree.rebased_branch(t) == "feature/x" and worktree.carried_branch(t) == ""
    path, branch = worktree.ensure(p, t)
    assert branch.startswith(f"ttp/t{t['id']}-")
    assert _git_out(path, "rev-parse", "HEAD") == _git_out(p.root, "rev-parse", "feature/x")
    assert "rebases branch feature/x" in worker_task(p, t, str(path), branch)
    # A title without "rebase" keeps pr_branch.
    assert coord.apply(p, [{"type": "task_add", "title": "Fix feature/x CI", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nFix it."}]) == []
    assert json.loads(p.db.one("SELECT labels FROM tasks WHERE title='Fix feature/x CI'")["labels"]) \
        == ["pr_branch:feature/x"]
    # Its review: retargeted to the task's own branch, published with --own, review only for the queue.
    code, _, _, (rev,) = _finish_code(env, p, "rebase it", {"r.py": 3}, labels=["rebases:feature/x"])
    own = p.db.task(code)["branch"]
    assert own.startswith(f"ttp/t{code}-") and push.pr_elsewhere(p, p.db.task(code), {"push_branch": "origin/work"}) == own
    assert f"It rebases feature/x and delivers onto its own branch {own}" in rev["spec"], rev["spec"]
    assert "`ttp push --own --detach`" in rev["spec"] and "do not approve it for the push queue" in rev["spec"]


def test_own_push_off_keeps_own_out_of_daemon_specs_and_a_rebase_lands_through_the_queue(env, monkeypatch):
    """With own pushes off, a rebases: task's review, its worker prompt and review.auto_notes never
    mention --own; the review approves it for the push queue, which takes it."""
    from ttp import coordinator as coord, push, pushq
    from ttp.prompts import OWN_SECTION, worker_task
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "off")
    p.set_config("review.auto_notes", "Run the linter. If it passes, publish it with `ttp push --own --detach`.")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    _git_out(p.root, "branch", "feature/x")
    assert coord.apply(p, [{"type": "task_add", "title": "Rebase feature/x onto work", "kind": "code",
                            "spec": "pr_branch: `feature/x`\nRebase it onto the latest work."}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='Rebase feature/x onto work'")
    assert json.loads(t["labels"]) == ["rebases:feature/x"]
    notes = " ".join(p.db.kv(coord.NOTES_KEY) or [])
    assert "own pushes are off" in notes and "push --own`" not in notes.replace("never write `--own`", "")
    prompt = worker_task(p, t, str(p.root), f"ttp/t{t['id']}-rebase")
    assert "own pushes are off" in prompt and "own pushes=off" in prompt
    code, _, _, (rev,) = _finish_code(env, p, "rebase it", {"r.py": 3}, labels=["rebases:feature/x"])
    d = p.config()["delivery"]
    assert push.pr_elsewhere(p, p.db.task(code), d) == ""
    assert "--own" not in rev["spec"] and "Run the linter." in rev["spec"], rev["spec"]
    assert "own pushes are off" in rev["spec"] and push.REVIEW_ONLY_LABEL not in json.loads(rev["labels"])
    assert OWN_SECTION not in worker_task(p, rev, str(p.root), None)
    # The queue takes the review's approval instead of sending it to --own; with own pushes on it would not.
    p.set_config("delivery.push_queue", True)
    approval = [{"branch": p.db.task(code)["branch"], "head": "ab" * 20}]
    assert "delivers onto its pr_branch" not in str(pushq.check_approval(p, rev, approval))
    p.set_config("delivery.own_push", "on")
    assert "delivers onto its pr_branch" in str(pushq.check_approval(p, rev, approval))


def test_own_push_existing_only_reviews_a_branch_the_remote_lacks_only_when_no_queue_takes_it(env, monkeypatch):
    """delivery.own_push existing_only with the push queue off: a pr_branch the push remote has no
    branch of cannot be created with `ttp push --own`, so its review is review only, not an own push.
    One the remote has keeps the --own step; a rebase onto its own new branch is review only too."""
    from ttp import push, pushq
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("delivery.push_branch", "origin/work")
    p.set_config("delivery.own_push", "existing_only")
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(Daemon, "_precheck", lambda *a: None)
    _, _, _, (gone,) = _finish_code(env, p, "gone", {"g.py": 3}, labels=["pr_branch:feature/x"])
    assert "--own" not in gone["spec"] and "Review only" in gone["spec"], gone["spec"]
    assert "origin has no branch feature/x" in gone["spec"] and "existing_only" in gone["spec"]
    assert "the pushed commit" not in gone["spec"] and push.REVIEW_ONLY_LABEL in json.loads(gone["labels"])
    # With the queue on, a rebase still has no queue path (it never lands on the push branch): review only.
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: True)
    _git_out(p.root, "branch", "feature/y")
    _, _, _, (reb,) = _finish_code(env, p, "rebase y", {"y.py": 3}, labels=["rebases:feature/y"])
    assert "--own" not in reb["spec"] and "Review only" in reb["spec"], reb["spec"]
    # A pr_branch the remote has: its PR's branch, --own as before.
    monkeypatch.setattr(pushq, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(pushq, "_absent", lambda *a: False)
    _, _, _, (there,) = _finish_code(env, p, "there", {"t.py": 3}, labels=["pr_branch:feature/x"])
    assert "`ttp push --own --detach`" in there["spec"] and "Review only" not in there["spec"], there["spec"]


def test_after_push_uses_the_push_module_the_process_started_with(monkeypatch):
    """The regression: run_after reaches batch.after_ordinary_push through the batch module loaded at
    start. A batch.py swapped on disk mid-push (an upgrade) is not imported into the running process."""
    import ttp.batch
    from ttp import push
    assert push.batch is ttp.batch and ttp.batch.push is push
    calls = []
    monkeypatch.setattr(ttp.batch, "after_ordinary_push", lambda *a: calls.append(a) or {"status": "ok"})
    monkeypatch.delitem(sys.modules, "ttp.batch")   # as if the file on disk were new: a re-import would differ
    monkeypatch.setattr(push, "pending_after", ("a" * 40, "origin/b", "b" * 40))
    assert push.run_after(None, pathlib.Path(".")) == {"status": "ok"}
    assert calls and "ttp.batch" not in sys.modules


@pytest.mark.parametrize("rc,output", [(2, "python3: can't open file 'tests/test_absent.py': [Errno 2] No such file or directory"),
                                     (4, "ERROR: file or directory not found: tests/test_absent.py"),
                                     (5, "no tests collected in 0.01s")])
def test_missing_check_target_requires_tree_evidence_and_preserves_related_failure(env, rc, output):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    (p.root / "README.md").write_text("updated docs\n")
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "docs")
    check = push.Check("python3 -m pytest tests/test_absent.py")
    failure = push.target_missing(p.root, "HEAD", check, rc, output, base)
    assert failure and failure["kind"] == "target_missing" and failure["unrelated"] is True
    # Untracked files and files in another commit must not make a missing tree target present.
    (p.root / "tests").mkdir(exist_ok=True)
    (p.root / "tests/test_absent.py").write_text("def test_ok(): pass\n")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base)
    _git_out(p.root, "add", ".")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test added")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base) is None
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    _git_out(p.root, "rm", "tests/test_absent.py")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "test deleted")
    assert push.target_missing(p.root, "HEAD", check, rc, output, base)["unrelated"] is False
    assert push.target_missing(p.root, "HEAD", check, 1, "FAILED test_assertion", base) is None


@pytest.mark.parametrize("gap_kind", ["target_missing", "selector_missing"])
def test_missing_check_gap_has_one_owner_and_waits_across_three_reviews(env, monkeypatch, gap_kind):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    command = "python3 -m pytest tests/test_absent.py"
    if gap_kind == "selector_missing":
        (p.root / "scripts").mkdir()
        _commit_files(p.root, {"scripts/check_selector.py": "# mandatory selector\n"}, "selector")
        command = "python3 scripts/check_selector.py"
    p.set_config("delivery.push_checks", [command, "true"])
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    reviews, consumers = [], []
    for i in range(3):
        code, branch, _, (rev,) = _finish_code(env, p, f"docs {i}", {f"docs/{i}.md": 1})
        consumer = p.db.add_task(f"consumer {i}", depends_on=[rev["id"]])
        run = env["tmp"] / f"missing-{i}"
        run.mkdir()
        path = p.worktrees / f"t{code}"
        head = _git_out(path, "rev-parse", "HEAD").strip()
        output = (f"mandatory check selector: no case for HEAD {head}" if gap_kind == "selector_missing"
                  else "ERROR: file or directory not found: tests/test_absent.py")
        failure = push.applicability_failure(path, head, push.Check(command),
                                            1 if gap_kind == "selector_missing" else 4, output, base)
        (run / "check_failure.json").write_text(json.dumps(failure))
        (run / "checks.json").write_text(json.dumps({"commands": [command, "true", "git diff --check"]}))
        (run / "result.json").write_text(json.dumps({"status": "failed", "review_failure": "check_only",
                                                   "summary": "only the check target is missing",
                                                   "on_pass": {"status": "done", "summary": "review complete; checks passed"}}))
        # A real run row permits normal model-free check settlement after the config repair.
        p.db.x("INSERT INTO runs(task,role,status,dir) VALUES(?,?,?,?)", (rev["id"], "worker", "ok", str(run)))
        d = Daemon(p.base)
        d._finish_worker({"task": rev["id"], "note": "{}"}, RunUsage(), "ok", run)
        d.reconcile_tasks()
        reviews.append(rev["id"])
        consumers.append(consumer)
    gaps = p.db.kv(checkgaps.KEY)
    assert len(gaps) == 1
    gap = next(iter(gaps.values()))
    owners = p.db.q("SELECT * FROM tasks WHERE kind='harness' AND origin='daemon'")
    assert len(owners) == 1 and gap["owner"] == owners[0]["id"]
    assert set(gap["tasks"]) == set(map(str, reviews))
    for tid in reviews:
        task = p.db.task(tid)
        prev = json.loads(task["result"])
        assert task["status"] == "queued" and prev["status"] == "waiting"
        assert "checks --gap" in prev["retry_when"] and task["attempts"] == 0
    assert all(p.db.task(t)["status"] == "queued" for t in consumers)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, name, tid, extra: started.append((tid, extra)) or True)
    d.reconcile_tasks()
    assert not started
    repaired = [command + " --updated-head-cases", "true"] if gap_kind == "selector_missing" else ["true"]
    p.set_config("delivery.push_checks", repaired)
    d._load_config()
    d.reconcile_tasks()
    assert {tid for tid, _ in started} == set(reviews)
    assert all(extra == ["--fresh", "--", "git diff --check"] for _, extra in started)
    assert all("checks --result" in json.loads(p.db.task(t)["result"])["retry_when"] for t in reviews)
    d.reconcile_tasks()
    assert len(started) == 3, "a restart or another tick must not start the same rerun again"

    # Explicit review verdicts settle only after a passing record for each clean, unchanged tree.
    for tid in reviews:
        member = gap["tasks"][str(tid)]
        out = pathlib.Path(member["run_dir"])
        (out / "checks.rc").write_text("0\n")
        (out / "checks.json").write_text(json.dumps({"passed": True, "head": member["head"],
                                                    "worktree": member["worktree"], "commands": repaired}))
        d._probe_passed(p.db.task(tid), time.time())
        assert p.db.task(tid)["status"] == "done"
    assert all(p.db.task(t)["status"] == "queued" for t in consumers)


def test_missing_check_target_infers_component_scope_without_skipping(env):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    check = push.Check("python3 -m pytest component/tests/test_absent.py")
    output = "ERROR: file or directory not found: component/tests/test_absent.py"
    (p.root / "other").mkdir()
    (p.root / "component").mkdir()
    _commit_files(p.root, {"other/app.py": "other\n"}, "other")
    assert push.target_missing(p.root, "HEAD", check, 4, output, base)["unrelated"] is True
    _commit_files(p.root, {"component/app.py": "related\n"}, "related")
    assert push.target_missing(p.root, "HEAD", check, 4, output, base)["unrelated"] is False
    assert push.applicable(p.root, "HEAD", [check], lambda _: None, base)[0] == [check]


def test_scoped_check_cannot_skip_a_missing_target_on_related_changes(env):
    from ttp import push
    p = make(env)
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    checks = push.check_list([{"run": "python3 -m pytest tests/test_absent.py",
                              "if_exists": "tests/test_absent.py", "if_changed": ["src/**", "tests/**"]}])
    _commit_files(p.root, {"README.md": "docs\n"}, "docs")
    assert not push.applicable(p.root, "HEAD", checks, lambda _: None, base)[0]
    (p.root / "src").mkdir()
    _commit_files(p.root, {"src/app.py": "code\n"}, "code")
    with pytest.raises(push.ScopeError, match="inside the check's scope"):
        push.applicable(p.root, "HEAD", checks, lambda _: None, base)
    with pytest.raises(push.ScopeError):
        push.skip_reason(p.root, "HEAD", checks[0], base)


@pytest.mark.parametrize("finding,related", [("check_only", True), ("mixed", False), ("substantive", False)])
def test_related_or_substantive_missing_check_failure_still_fails(env, finding, related):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    command = "python3 -m pytest tests/test_absent.py"
    p.set_config("delivery.push_checks", [command])
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    code, _, _, (rev,) = _finish_code(env, p, "change", {"app.py" if related else "NOTES.md": 1})
    path = p.worktrees / f"t{code}"
    head = _git_out(path, "rev-parse", "HEAD").strip()
    run = env["tmp"] / "failed-check-review"
    run.mkdir()
    failure = push.target_missing(path, head, push.Check(command), 4,
                                  "ERROR: file or directory not found: tests/test_absent.py", base)
    (run / "check_failure.json").write_text(json.dumps(failure))
    (run / "result.json").write_text(json.dumps({"status": "failed", "review_failure": finding,
                                               "summary": "review failed"}))
    Daemon(p.base)._finish_worker({"task": rev["id"], "note": "{}"}, RunUsage(), "ok", run)
    assert p.db.task(rev["id"])["status"] == "failed"
    assert not p.db.kv(checkgaps.KEY, {})


@pytest.mark.parametrize("diagnostic", ["mandatory check selector: no case for HEAD {head}",
                                        "check selector: no matching case for HEAD {head}",
                                        "ERROR: no verification case for HEAD {head}"])
def test_unmatched_check_selector_requires_the_exact_tested_head(env, diagnostic):
    from ttp import push
    p = make(env)
    (p.root / "scripts").mkdir()
    _commit_files(p.root, {"scripts/select.py": "# mandatory selector\n"}, "selector")
    head = _git_out(p.root, "rev-parse", "HEAD").strip()
    check = push.Check("python3 scripts/select.py")
    output = diagnostic.format(head=head)
    failure = push.applicability_failure(p.root, head, check, 1, output)
    assert failure and failure["kind"] == "selector_missing"
    assert failure["targets"] == [head] and failure["unrelated"] is True
    assert push.applicable(p.root, head, [check, push.Check("git diff --check")], lambda _: None)[0] == [check, "git diff --check"]
    for rc, text in [(0, output), (1, diagnostic.format(head="a" * 40)),
                     (1, "selector assertion failed"), (1, "byte-equivalent tree; checks passed previously")]:
        assert push.applicability_failure(p.root, head, check, rc, text) is None


def test_applicability_retry_start_failure_remains_retryable(env, monkeypatch):
    from ttp import checkgaps, push
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config('delivery.push_checks', ['true'])
    original = checkgaps.config_sig(p.config())
    head = _git_out(p.root, 'rev-parse', 'HEAD').strip()
    tid = p.db.add_task('waiting review', kind='review')
    run = env['tmp'] / 'retry-run'
    run.mkdir()
    sig, key = 'retry-signature', 'check:retry-signature'
    result = {'status': 'waiting', 'check_gap': key, 'retry_when': checkgaps.probe(p, sig, original)}
    (run / 'result.json').write_text(json.dumps(result))
    p.db.update_task(tid, result=json.dumps(result))
    owner = p.db.add_task('repair', kind='harness', origin='daemon')
    p.db.set_kv(checkgaps.KEY, {key: {'config': original, 'check': push.Check('true').config(), 'owner': owner, 'tasks': {str(tid): {
        'run_dir': str(run), 'worktree': str(p.root), 'head': head, 'config': original, 'extras': []}}}})
    p.set_config('delivery.push_checks', ['git diff --check'])
    d = Daemon(p.base)
    monkeypatch.setattr(d, '_start_checks', lambda *args: False)
    checkgaps.rerun(d)
    assert p.db.kv(checkgaps.KEY)[key]['tasks'][str(tid)]['config'] == original
    assert json.loads(p.db.task(tid)['result']) == result
    started = []
    monkeypatch.setattr(d, '_start_checks', lambda *args: started.append(args) or True)
    checkgaps.rerun(d)
    assert len(started) == 1
    assert 'checks --result' in json.loads(p.db.task(tid)['result'])['retry_when']


def _deploy_release_with_every_pending_state(tmp_path):
    """A release whose lagging projects are each pending with a known owner: two merges held by harness
    tasks, one held while that project pushes (a harness task too), one daemon restart pending."""
    from ttp import release
    _deploy_lib('0.5.0')
    dbs = {'self': _deploy_project(tmp_path, 'self', '0.5.0')[1]}   # the reporting project, upgraded
    for name in ('alpha', 'gamma', 'delta'):
        _, dbs[name] = _deploy_project(tmp_path, name, '0.4.9')
        dbs[name].add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    _, dbs['beta'] = _deploy_project(tmp_path, 'beta', '0.5.0', running='0.4.9')   # restart pending
    return dbs


def test_after_push_with_every_lagging_project_owned_pending_exits_0_with_one_low_note(env, tmp_path, monkeypatch, capsys):
    """Replays a deploy where every project behind is pending with an owner (merges held by harness
    tasks, a restart pending): `ttp deployed --after-push` exits 0, one low note, no alert, no page."""
    from ttp import pushq
    from ttp.cli import main
    p = make(env)
    names = list(_deploy_release_with_every_pending_state(tmp_path))
    monkeypatch.setenv('TTP_PROJECT', str(p.base))
    started = time.time()
    with pytest.raises(SystemExit) as e:
        main(['deployed', '0.5.0', '--commit', DEPLOY_C, '--projects', ','.join(names), '--after-push', '--json'])
    data = json.loads(capsys.readouterr().out)
    assert e.value.code == 0
    assert {r['name']: r['outcome'] for r in data['projects']} == {
        'self': 'done', 'alpha': 'pending', 'beta': 'pending', 'gamma': 'pending', 'delta': 'pending'}
    out = p.db.q("SELECT kind,severity,text FROM messages WHERE direction='out'")
    assert [(m['kind'], m['severity']) for m in out] == [('info', 'low')]
    assert all(n in out[0]['text'] for n in ('alpha', 'beta', 'gamma', 'delta'))
    assert not p.db.q('SELECT * FROM alerts')
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', started))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0', 'after_push': {
        'exit': e.value.code, 'started': started, 'ended': time.time()}}, 'ok', lambda *a, **k: pages.append(a), time.time())
    assert not pages


@pytest.mark.parametrize('down', [['beta'], ['alpha', 'beta']])
def test_after_push_deploy_real_failure_sends_exactly_one_alert(env, tmp_path, monkeypatch, capsys, down):
    """A daemon that is down still fails the deploy: one alert per project behind (high), and the push
    queue sends no after_push alert of its own."""
    from ttp import deploy, pushq
    from ttp.cli import main
    p = make(env)
    dbs = _deploy_release_with_every_pending_state(tmp_path)
    names = list(dbs)
    for name in down:
        dbs[name].set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    monkeypatch.setenv('TTP_PROJECT', str(p.base))
    started = time.time() - 60
    with pytest.raises(SystemExit) as e:
        main(['deployed', '0.5.0', '--commit', DEPLOY_C, '--projects', ','.join(names), '--after-push', '--json'])
    capsys.readouterr()
    assert e.value.code == 1
    alerts_out = p.db.q("SELECT severity,ref,text FROM messages WHERE kind='alert' ORDER BY id")
    assert [(a['severity'], a['ref']) for a in alerts_out] == [('high', f'deploy-behind:{n}') for n in down]
    assert all(n in a['text'] for n, a in zip(down, alerts_out))
    assert [a['key'] for a in p.db.q('SELECT key FROM alerts ORDER BY id')] == [f'deploy-behind:{n}' for n in down]
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', started))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0', 'after_push': {
        'exit': 1, 'started': started, 'ended': time.time()}}, 'failed', lambda *a, **k: pages.append(a), time.time())
    assert not pages, 'one deploy outcome, one alert'
    assert p.db.one("SELECT id FROM events WHERE kind='after_push_failed'"), 'the coordinator still hears of it'
    pages = []
    deploy.tick(p, lambda *a, **k: pages.append(a), time.time() + deploy.PROBE_S)
    assert not pages, 'the probe after it does not repeat the alert'
    # An after_push failing for another reason (no deploy alert for that commit) still pages.
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-other', 'marker', 'origin/topic', started))
    pushq._after(p, {'id': 'batch-other'}, {'pushed_sha': '1234567890abcdef', 'after_push': {
        'exit': 1, 'started': started, 'ended': time.time()}}, 'failed', lambda *a, **k: pages.append(a), time.time())
    assert [a[0] for a in pages] == ['after_push_failed']


def test_deploy_alerted_key_is_shared_by_deploy_and_the_push_queue():
    from ttp import deploy, pushq
    assert pushq.DEPLOY_ALERTED == deploy.ALERTED


def test_after_push_deploy_pending_success_does_not_page_push_queue(env, tmp_path):
    from ttp import deploy, overview, pushq, release
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    rc = deploy.record(p, overview.deployed(names=['alpha']))['rc']
    now = time.time()
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
           ('batch-test', 'marker', 'origin/topic', now))
    pages = []
    pushq._after(p, {'id': 'batch-test'}, {'pushed_sha': DEPLOY_C, 'version': '0.5.0',
                 'after_push': {'exit': rc, 'started': now, 'ended': now}},
                 'ok' if rc == 0 else 'failed', lambda *a, **k: pages.append(a), now)
    assert not pages and not p.db.one("SELECT id FROM events WHERE kind='after_push_failed'")
    assert p.db.one("SELECT after_push FROM push_batches")['after_push'] == 'ok'


def test_a_project_stuck_across_releases_is_one_alert_tracking_the_newest(env, tmp_path):
    """Replays alerts #469-#476: a project stuck behind release after release got one alert per release.
    It is one ongoing alert per project, updated in place with the newest release it misses, that clears
    once its own daemon reports the release; two stuck projects are two alerts; the after_push failure of
    each deploy sends none of its own."""
    from ttp import alerts, deploy, overview, pushq
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    _deploy_lib('0.4.9')
    a_base, a_db = _deploy_project(tmp_path, 'alpha', '0.4.8')
    _deploy_project(tmp_path, 'beta', '0.4.8')
    pages, page = _deploy_pager(p)
    now = time.time()
    versions = [f'0.5.{i}' for i in range(6)]
    for i, v in enumerate(versions):
        t = now + 10 * i
        _deploy_lib(v)
        started = t - 5
        assert deploy.record(p, overview.deployed(names=['alpha', 'beta']), t)['rc'] == 1
        p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES(?,?,?,?)",
               (f'batch-{v}', 'marker', 'origin/topic', started))
        pushq._after(p, {'id': f'batch-{v}'}, {'pushed_sha': DEPLOY_C, 'version': v, 'after_push': {
            'exit': 1, 'started': started, 'ended': t}}, 'failed', lambda *a, **k: pages.append(a), t)
        deploy.tick(p, page, t + 1)
        deploy.tick(p, page, t + 1 + deploy.PROBE_S)
        out = p.db.q("SELECT ref,text FROM messages WHERE kind='alert' ORDER BY id")
        assert [m['ref'] for m in out] == ['deploy-behind:alpha', 'deploy-behind:beta'], (v, out)
        assert all(f'tt-project {v} ' in m['text'] for m in out), out
        assert all(f'tt-project {old} ' not in m['text'] for m in out for old in versions[:i])
        live = p.db.q("SELECT key,text FROM alerts WHERE cleared IS NULL ORDER BY id")
        assert [a['key'] for a in live] == ['deploy-behind:alpha', 'deploy-behind:beta']
        assert all(f'tt-project {v} ' in a['text'] and 'behind since' in a['text'] for a in live)
        open_checks = [r for r in p.db.kv(deploy.CHECKS).values() if not r.get('closed')]
        assert sorted((r['name'], r['target']) for r in open_checks) == [('alpha', v), ('beta', v)]
        assert {r['since'] for r in open_checks} == {now}, 'behind since its first missed release'
    assert not pages, 'no new page per release, and no after_push alert of its own'
    t = now + 10 * len(versions)
    _deploy_catch_up(a_base, a_db, versions[-1], DEPLOY_C, t)
    assert not alerts.holds(p.db, 'deploy-behind:alpha', now, t)
    assert alerts.holds(p.db, 'deploy-behind:beta', now, t)
    assert [e['key'] for e in alerts.sweep(p.db, now=t)] == ['deploy-behind:alpha']
    deploy.tick(p, page, t)
    assert not pages and [a['key'] for a in p.db.q("SELECT key FROM alerts WHERE cleared IS NULL")] == \
        ['deploy-behind:beta']


@pytest.mark.parametrize("queued", [False, pytest.param(True, marks=pytest.mark.skipif(
    sys.platform != "linux", reason="a launcher finds itself boxed only by its Linux PID namespace or cgroup "
    "(/proc); other hosts are never boxed and take the direct path"))], ids=["direct", "daemon-queued"])
def test_detached_checks_temp_survives_worker_cleanup(env, tmp_path, monkeypatch, queued, request,
                                                    reap_tmp_processes):
    from ttp import cli, detach, push, runner

    def reap():
        # The checks and their command (a session of its own) block on `release` until the test
        # writes it: however the test ends, nothing they started may outlive it.
        assert reap_tmp_processes(tmp_path) == [], "the detached checks outlived the test"
    request.addfinalizer(reap)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    worker_tmp = run / "tmp"
    worker_tmp.mkdir()
    (run / "run.json").write_text(json.dumps({"tmp_dir": str(worker_tmp)}))
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(worker_tmp))
    ready, release = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(release)
    code = ("import os, pathlib, tempfile; "
            "f = pathlib.Path(tempfile.mkdtemp()) / 'keep'; f.write_text('alive'); "
            f"pathlib.Path({str(ready)!r}).write_text(str(f)); "
            f"open({str(release)!r}).read(); "
            "assert f.read_text() == 'alive'; "
            "assert all(os.environ[k] == tempfile.gettempdir() for k in ('TMPDIR', 'TMP', 'TEMP'))")
    # Real marker mismatch, not a mock of _boxed: the disposable command queues, exits,
    # and leaves no child with a sandbox-local pid for the daemon to misidentify.
    launch = subprocess.run([sys.executable, "-m", "ttp", "checks", "--detach", "--",
                             shlex.join([sys.executable, "-c", code])], cwd=repo,
                            env={**os.environ, "PYTHONPATH": str(RUNTIME),
                                 **({"TTP_PIDNS": "pid:[disposable-command]"} if queued else {})},
                            capture_output=True, text=True, timeout=30)
    assert launch.returncode == 0, launch.stderr
    if queued:
        assert "queued" in launch.stdout and not (run / cli.CHECKS_PID).exists()
        detach.tend(p)
    info = cli._read_checks_pid(run)
    assert _wait_for(ready.exists), (run / cli.CHECKS_OUT).read_text()
    scratch = pathlib.Path(ready.read_text()).parent.parent
    runner.remove_private(run)
    assert not worker_tmp.exists()
    assert pathlib.Path(ready.read_text()).exists(), "worker cleanup deleted live checks temp files"
    monkeypatch.setattr(p, "runs", tmp_path)
    detach.tend(p)   # recovery must leave live checks' storage alone
    assert pathlib.Path(ready.read_text()).exists()
    release.write_text("go")
    assert _wait_precheck(run) == "0"
    assert _wait_for(lambda: not scratch.exists()), "completed checks left scratch behind"
    assert cli._checks_result(run) == 0
    assert info["pidns"] == push.pid_ns()


def test_leak_guard_finds_and_reaps_processes_naming_the_temp_root(tmp_path, leaked_tmp_processes,
                                                                    reap_tmp_processes):
    """The session guard (tests/conftest.py) reports a process left running that names the temp root,
    whether detached or in pytest's own process group, never one naming a sibling path, and stops
    them without touching pytest."""
    sleep = "import sys, time; time.sleep(120)"
    detached = subprocess.Popen([sys.executable, "-c", sleep, str(tmp_path / "a")], start_new_session=True)
    same_group = subprocess.Popen([sys.executable, "-c", sleep, str(tmp_path)])
    sibling = subprocess.Popen([sys.executable, "-c", sleep, f"{tmp_path}1/b"], start_new_session=True)
    try:
        want = {detached.pid, same_group.pid}
        assert _wait_for(lambda: {pid for pid, _, _ in leaked_tmp_processes(tmp_path)} == want), \
            leaked_tmp_processes(tmp_path)
        assert {pid: pgid for pid, pgid, _ in leaked_tmp_processes(tmp_path)}[detached.pid] == detached.pid
        assert reap_tmp_processes(tmp_path) == []
        assert detached.wait(timeout=10) == -signal.SIGTERM and same_group.wait(timeout=10) == -signal.SIGTERM
        assert sibling.poll() is None, "a process naming a sibling path was stopped"
        assert [pid for pid, _, _ in leaked_tmp_processes(f"{tmp_path}1")] == [sibling.pid]
    finally:
        for proc in (detached, same_group, sibling):
            proc.kill()
            proc.wait()


def test_sandboxed_detached_checks_without_daemon_refused_before_launch(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.delenv("TTP_PROJECT")
    monkeypatch.setattr(push, "pid_ns", lambda: "pid:[host]")   # a PID namespace on every OS (macOS has none)
    monkeypatch.setenv("TTP_PIDNS", "pid:[disposable-command]")
    with pytest.raises(SystemExit) as refused:
        cli.main(["checks", "--detach", "--", "true"])
    assert refused.value.code == 2
    assert "need a project daemon" in capsys.readouterr().err
    assert not (run / cli.CHECKS_PID).exists() and not (run / cli.CHECKS_TMP).exists()


def test_detached_checks_start_directly_where_there_is_no_pid_namespace(env, tmp_path, monkeypatch, capsys):
    """macOS has no /proc: no PID namespace or cgroup to tell a sandbox by, so a launcher is never
    boxed, even with a run's markers set, and starts its checks itself instead of queueing them."""
    from ttp import cli, detach, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, "pid_ns", lambda: None)
    monkeypatch.setattr(push, "cgroup", lambda: None)
    monkeypatch.setenv("TTP_PIDNS", "pid:[run]")
    monkeypatch.setenv("TTP_CGROUP", "0::/run")
    assert push._boxed() is False
    _detached_checks(run, capsys, "true")
    assert not list((p.state / detach.QUEUE).glob("*.json")), "queued for a daemon instead of started"
    assert _wait_precheck(run) == "0" and cli._read_checks_pid(run)["pidns"] is None
    assert cli._checks_result(run) == 0


@pytest.mark.parametrize('run_env', [None, ''], ids=['unset', 'empty'])
def test_check_output_queued_push_rebuilt_after_restart(env, monkeypatch, capsys, run_env):
    from ttp import push
    from ttp.project import Project
    command = _check_output_paths_command()
    p, repo, origin, other = _push_setup(env, monkeypatch, [command])
    _commit(repo, 'mine.txt', 'mine\n')
    monkeypatch.setattr(push, '_boxed', lambda: True)
    pending = []
    for _ in range(2):
        assert _ttp('push', '--detach') == 0
        out = capsys.readouterr().out
        marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in out.splitlines() if ln.startswith('marker: ')))
        m = json.loads(marker.read_text())
        assert m.get('check_run_dir'), 'queue must persist the check output directory'
        output = pathlib.Path(m['check_run_dir'])
        pending.append((marker, output))
        shutil.rmtree(output)
    assert pending[0][1] != pending[1][1], 'queued invocations must not collide'
    if run_env is None:
        monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', run_env)
    monkeypatch.setenv('TTP_PIDNS', 'must not inherit')
    monkeypatch.setenv('TTP_CGROUP', 'must not inherit')
    push.tend(Project(p.base))  # restart reads only persisted queue data, without the worker's env
    for marker, output in pending:
        result = _probe_until_done(p, push.result_probe(marker, p))
        assert 'pushed' in result.stdout, result.stdout
        # Rebuilt storage has no owner record. The narrowed cleanup contract leaves it alone.
        assert _wait_for(lambda: json.loads(marker.read_text()).get('ended'))
        assert output.exists() and not (output / 'owner.json').exists()
        reports = pathlib.Path(json.loads(marker.read_text())['check_report_dir'])
        assert reports.parent == p.state / 'check-reports'
        assert (reports / 'pre-commit-cache').is_dir()
        paths = json.loads((reports / 'result.json').read_text())
        assert paths == {'output': str(reports), 'scratch': str(output / 'checks-tmp')}, \
            'a rebuilt allocation has no owner.json naming a short dir: it keeps checks-tmp'


@pytest.mark.parametrize('run_env', [None, ''], ids=['unset', 'empty'])
def test_check_output_queued_checks_after_restart_without_run_env(env, tmp_path, monkeypatch, capsys, run_env):
    from ttp import cli, detach, push, shorttmp
    from ttp.daemon import Daemon
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(push, 'pid_ns', lambda: 'pid:[host]')   # a PID namespace on every OS (macOS has none)
    monkeypatch.setenv('TTP_PIDNS', 'disposable-command')
    cli.main(['checks', '--detach', '--', _check_output_paths_command()])
    assert 'queued' in capsys.readouterr().out
    assert not (run / cli.CHECKS_PID).exists()
    q = next((p.state / detach.QUEUE).glob('*.json'))
    record = json.loads(q.read_text())
    if run_env is None:
        monkeypatch.delenv('TTP_RUN_DIR')
        record['env'].pop('TTP_RUN_DIR', None)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', run_env)
        record['env']['TTP_RUN_DIR'] = run_env
    q.write_text(json.dumps(record))
    monkeypatch.delenv('TTP_PIDNS')
    Daemon(p.base).tend_pushes()  # a fresh daemon has only the persisted request
    assert _wait_precheck(run) == '0', (run / cli.CHECKS_OUT).read_text()
    info = cli._read_checks_pid(run)
    reports = pathlib.Path(info['check_report_dir'])
    assert reports.is_relative_to(p.state) and not reports.is_relative_to(repo)
    assert (reports / 'pre-commit-cache').is_dir()
    paths = json.loads((reports / 'result.json').read_text())
    assert paths['output'] == str(reports) and shorttmp.owned(paths['scratch']), paths
    assert _wait_for(lambda: not pathlib.Path(paths['scratch']).exists()), 'removed with its allocation'
    assert not q.exists()


@pytest.mark.parametrize('value', [None, '', '/', '//', 'relative'])
def test_check_output_missing_or_invalid_fails_closed(env, monkeypatch, value):
    from ttp import push
    if value is None:
        monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    else:
        monkeypatch.setenv('TTP_RUN_DIR', value)
    with pytest.raises((OSError, ValueError), match='TTP_RUN_DIR'):
        push.check_env('target')


def test_check_output_batch_child_has_own_writable_directory(env, monkeypatch):
    from ttp import pushq
    command = ('test -n "$TTP_RUN_DIR"; test "$TTP_RUN_DIR" != /; '
               'test -z "${TTP_PIDNS:-}"; test -z "${TTP_CGROUP:-}"; '
               'echo report > "$TTP_RUN_DIR/x"')
    s = _pq_real(env, monkeypatch, push_checks=[command])
    monkeypatch.setenv('TTP_PIDNS', 'worker-only')
    monkeypatch.setenv('TTP_CGROUP', 'worker-only')
    _pq_hand_off(env, s)
    bid = _pq_batch(s)
    marker = s.p.state / 'pushes' / f'{bid}.json'
    m = json.loads(marker.read_text())
    assert m['outcome'] == 'pushed', marker.with_suffix('.log').read_text()
    output = pathlib.Path(m['check_run_dir'])
    assert not output.exists()
    reports = pathlib.Path(m['check_report_dir'])
    assert reports.is_relative_to(s.p.state) and (reports / 'x').read_text() == 'report\n'


@pytest.mark.parametrize('bad', ['symlink', 'file', 'root', 'missing'])
def test_check_output_bad_queue_directory_runs_no_check(env, monkeypatch, capsys, bad):
    from ttp import push
    p, repo, origin, other = _push_setup(env, monkeypatch, ['true'])
    _commit(repo, 'mine.txt', 'mine\n')
    monkeypatch.setattr(push, '_boxed', lambda: True)
    assert _ttp('push', '--detach') == 0
    out = capsys.readouterr().out
    marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in out.splitlines() if ln.startswith('marker: ')))
    m = json.loads(marker.read_text())
    output = pathlib.Path(m['check_run_dir'])
    shutil.rmtree(output)
    if bad == 'symlink':
        output.symlink_to(repo, target_is_directory=True)
    elif bad == 'file':
        output.write_text('keep')
    elif bad == 'root':
        m['check_run_dir'] = '/'
    else:
        del m['check_run_dir']
    marker.write_text(json.dumps(m))
    real_popen = push.subprocess.Popen
    def never(argv, *a, **kw):
        if argv == ['git', '--version']:
            return real_popen(argv, *a, **kw)
        pytest.fail('started a check with invalid output storage')
    monkeypatch.setattr(push.subprocess, 'Popen', never)
    push.tend(p)
    m = json.loads(marker.read_text())
    assert m['status'] == 'failed' and 'TTP_RUN_DIR' in m['reason']


def test_check_output_missing_in_batch_process_fails_before_checks(env, monkeypatch):
    from ttp import batch, push
    check = env['tmp'] / 'must-not-run'
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f'touch {shlex.quote(str(check))}'])
    heads = [_entry(repo, 'e1', {'feature.txt': 'feature'})]
    marker = _batch_marker(p, heads)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    monkeypatch.delenv('TTP_RUN_DIR', raising=False)
    assert batch.run_batch(p, marker) != 0
    m = push._read(marker)
    assert m['outcome'] == 'error' and 'TTP_RUN_DIR' in m['message']
    assert not check.exists()


@pytest.mark.parametrize('mode', ['detached', 'batch-env'])
def test_check_scratch_git_discovery_stops_before_project(env, tmp_path, monkeypatch, mode):
    from ttp import cli, checkdirs, pushq
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    subprocess.run(['git', 'init', '-q', str(p.root)], check=True)
    inherited = str(tmp_path / 'existing-ceiling')
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', inherited)
    code = ("import os, subprocess, tempfile; "
            f"assert {inherited!r} in os.environ['GIT_CEILING_DIRECTORIES'].split(os.pathsep); "
            "r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=tempfile.gettempdir(), capture_output=True); "
            "assert r.returncode != 0, r.stdout; "
            f"r = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd={str(repo)!r}, capture_output=True, text=True); "
            f"assert r.returncode == 0 and r.stdout.strip() == {str(repo)!r}")
    if mode == 'detached':
        cli.main(['checks', '--detach', '--fresh', '--', shlex.join([sys.executable, '-c', code])])
        assert _wait_precheck(run) == '0', (run / cli.CHECKS_OUT).read_text()
    else:
        output = checkdirs.new(p.state)
        with checkdirs.take(output) as guard:
            child_env = pushq._child_env(p, str(output))
            child_env['TTP_CHECKS_TMP_FD'] = str(guard.fileno())
            r = subprocess.run([sys.executable, '-c', code], env=checkdirs.for_check(child_env),
                               pass_fds=(guard.fileno(),), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize('kind', ['push', 'batch'])
def test_check_scratch_push_and_batch_live_across_recovery(env, tmp_path, monkeypatch, capsys, kind):
    from ttp import checkdirs, push, pushq
    from ttp.project import Project
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    os.mkfifo(release)
    code = ("import os, pathlib, subprocess, tempfile; "
            "os.fstat(int(os.environ['TTP_CHECKS_TMP_FD'])); "
            "f = pathlib.Path(tempfile.mkdtemp()) / 'keep'; f.write_text('alive'); "
            "assert subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=tempfile.gettempdir(), capture_output=True).returncode != 0; "
            "assert subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True).returncode == 0; "
            f"pathlib.Path({str(ready)!r}).write_text(str(f)); "
            f"open({str(release)!r}).read(); assert f.read_text() == 'alive'; "
            "pathlib.Path(os.environ['TTP_RUN_DIR'], 'report').write_text('kept')")
    command = shlex.join([sys.executable, '-c', code])
    if kind == 'push':
        p, repo, origin, other = _push_setup(env, monkeypatch, [command])
        _commit(repo, 'mine.txt', 'mine\n')
        monkeypatch.setattr(push, '_boxed', lambda: True)
        assert _ttp('push', '--detach') == 0
        output = capsys.readouterr().out
        marker = pathlib.Path(next(ln.split(': ', 1)[1] for ln in output.splitlines() if ln.startswith('marker: ')))
        scratch = pathlib.Path(push._read(marker)['check_run_dir'])
        # No child or lifetime lock exists while queued; a new daemon must preserve the allocation.
        checkdirs.recover(p.state, p.runs)
        assert scratch.exists()
        push.tend(Project(p.base))
    else:
        s = _pq_real(env, monkeypatch, push_checks=[command])
        p = s.p
        _pq_hand_off(env, s)
        bid = _pq_batch(s, wait=False)
        marker = p.state / 'pushes' / f'{bid}.json'
        scratch = pathlib.Path(push._read(marker)['check_run_dir'])
    try:
        assert _wait_for(ready.exists), marker.with_suffix('.log').read_text()
        fresh = Project(p.base)
        checkdirs.recover(fresh.state, fresh.runs)
        assert pathlib.Path(ready.read_text()).read_text() == 'alive'
        assert checkdirs.take(scratch) is None, 'push/batch did not hold the scratch lifetime lock'
        release.write_text('go')
        if kind == 'push':
            result = _probe_until_done(p, push.result_probe(marker, p))
            assert 'pushed' in result.stdout, result.stdout
        else:
            assert pushq._children[bid].wait(timeout=30) == 0
        assert _wait_for(lambda: not scratch.exists())
        reports = pathlib.Path(push._read(marker)['check_report_dir'])
        assert (reports / 'report').read_text() == 'kept'
    finally:
        if kind == 'batch':
            child = pushq._children[bid]
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
        else:
            pid = push._read(marker).get('pid')
            if pid and not _gone_pid(pid):
                os.killpg(pid, signal.SIGKILL)


@pytest.mark.parametrize('own', [False, True])
def test_check_output_foreground_push_preserves_worker_context(env, monkeypatch, own):
    from ttp import push
    seen = env['tmp'] / 'seen'
    command = (f'printf "%s\\n%s\\n" "$TTP_RUN_DIR" "$TMPDIR" > {shlex.quote(str(seen))}; '
               'echo report > "$TTP_RUN_DIR/report"')
    p, repo, origin, other = _push_setup(env, monkeypatch, [command])
    worker = env['tmp'] / 'run'
    worker.mkdir()
    if own:
        _git_out(repo, 'checkout', '-qb', 'task/check-output')
    monkeypatch.setenv('TTP_RUN_DIR', str(worker))
    before = dict(os.environ)
    real = push.take

    def take(*a, **kw):
        assert os.environ['TTP_RUN_DIR'] == str(worker), 'push bookkeeping lost the worker run'
        return real(*a, **kw)

    monkeypatch.setattr(push, 'take', take)
    _commit(repo, 'mine.txt', 'mine\n')
    assert push.run(p, repo, own=own) == 0
    report, scratch = map(pathlib.Path, seen.read_text().splitlines())
    assert report != worker and report.is_relative_to(p.state / 'check-reports')
    assert (report / 'report').read_text() == 'report\n'
    assert not scratch.exists()
    assert not (worker / 'report').exists()
    assert dict(os.environ) == before


@pytest.mark.parametrize('kind', ['detached-check', 'foreground-push'])
def test_check_scratch_failed_launch_or_check_cleans_allocation(env, tmp_path, monkeypatch, kind):
    from ttp import cli, checkdirs, push
    if kind == 'detached-check':
        p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
        real_popen = cli.subprocess.Popen
        def launch(argv, *args, **kw):
            if isinstance(argv, list) and argv[:3] == [sys.executable, '-m', 'ttp']:
                raise OSError('test launch refused')
            return real_popen(argv, *args, **kw)
        monkeypatch.setattr(cli.subprocess, 'Popen', launch)
        with pytest.raises(SystemExit):
            cli.main(['checks', '--detach', '--', 'true'])
    else:
        p, repo, origin, other = _push_setup(env, monkeypatch, ['false'])
        _commit(repo, 'mine.txt', 'mine\n')
        assert push.run(p, repo) == push.CHECKS_FAILED
    assert not [f for f in (p.state / checkdirs.FOLDER).iterdir() if f.is_dir()]


def test_live_after_push_keeps_ordinary_temp_after_batch_supervisor_dies(env, tmp_path, monkeypatch):
    from ttp import checkdirs, pushq, push
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    os.mkfifo(release)
    normal = tmp_path / 'daemon-tmp'
    normal.mkdir()
    for key in ('TMPDIR', 'TMP', 'TEMP'):
        monkeypatch.setenv(key, str(normal))
    code = ('import os,json,pathlib,tempfile; '
            "f=pathlib.Path(tempfile.mkdtemp())/'keep'; f.write_text('alive'); "
            f'pathlib.Path({str(ready)!r}).write_text(json.dumps([os.getpid(),str(f)])); '
            f'open({str(release)!r}).read(); assert f.read_text() == "alive"')
    s = _pq_real(env, monkeypatch, after_push=[shlex.join([sys.executable, '-c', code])])
    _pq_hand_off(env, s)
    bid = _pq_batch(s, wait=False)
    marker = s.p.state / 'pushes' / f'{bid}.json'
    child = pushq._children[bid]
    after_pid = None
    try:
        assert _wait_for(ready.exists), marker.with_suffix('.log').read_text()
        after_pid, temp = json.loads(ready.read_text())
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        os.kill(after_pid, 0)
        checkdirs.recover(s.p.state, s.p.runs)
        assert pathlib.Path(temp).read_text() == 'alive'
        assert pathlib.Path(temp).is_relative_to(normal)
        assert not pathlib.Path(push._read(marker)['check_run_dir']).exists()
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        if after_pid:
            try:
                os.kill(after_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_task_already_waiting_on_a_missing_task_is_flagged_once_and_shown_broken(env):
    """State from before the check: the daemon raises one event per task, does not run the probe, `ttp landed`
    says it cannot tell, and status and the web app show the task as broken, not as a plan."""
    from ttp import coordinator as coord
    from ttp import landed
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    p = make(env)
    tid = p.db.add_task("ghost wait", "s", origin="user",
                        labels=coord.defer_labels(None, "landed:#0"))
    held = p.db.add_task("ghost hold", "s", origin="user")
    p.db.update_task(held, status="blocked", labels=json.dumps(["waits:when:ttp landed --task 77777"]))
    fine = p.db.add_task("fine wait", "s", origin="user", labels=coord.defer_labels(None, f"landed:#{held}"))
    d = Daemon(p.base)
    for _ in range(2):
        d.reconcile_tasks()
    ev = p.db.q("SELECT task, text, status FROM events WHERE kind='missing_task_ref' ORDER BY task")
    assert [e["task"] for e in ev] == [tid, held], ev
    assert "waits on #0, which does not exist" in ev[0]["text"] and ev[0]["status"] == "queued"
    assert "waits on #77777, which does not exist" in ev[1]["text"]
    assert coord.EFFORT_EVENT_TRIGGERS["missing_task_ref"] == "stuck"
    started = []
    d._start_probe = lambda tid_, probe, now, what="retry_when": (started.append(tid_),
                                                                   d._probe_rc.__setitem__(tid_, (1, now, probe)))
    d.probe_deferred(time.time())
    assert tid not in started and fine in started, started
    assert landed.check(p, p.root, None, 0)[0] == landed.CANNOT, "a probe on a missing task said 'not yet'"
    t = next(t for t in state_payload(p, p.db)["tasks"] if t["id"] == tid)
    assert t["starts"] == "broken: waits on #0, which does not exist", t
    out = status_text(p)
    assert f"  #{tid} broken: waits on #0, which does not exist: ghost wait" in out, out
    assert "starts when #0 lands" not in out
    assert "broken: waits on #0, which does not exist (start_when: landed:#0)" in coord.digest(p, {}, [], [])


def test_check_purpose_tag_is_validated_and_kept(tmp_path, monkeypatch):
    from ttp import push
    _fake_check_plan_ttp(tmp_path, monkeypatch, tmp_path / "log")
    harness = {"run": HARNESS_PLAN, "purpose": "harness"}
    gate = {"run": "true", "purpose": "candidate"}
    assert push.check_problems([harness, gate, "true"]) == []
    checks = push.check_list([harness, gate, "true"])
    assert [c.purpose for c in checks] == ["harness", "candidate", ""]
    assert [c.config() for c in checks] == [harness, gate, "true"]
    assert push.check_sig(checks[1]) != push.check_sig(push.Check("true")), "purpose is part of the signature"
    # An unknown purpose is a config error, never a silent skip.
    bad = push.check_problems([{"run": "true", "purpose": "publish"}])
    assert len(bad) == 1 and "purpose must be one of candidate, harness" in bad[0]
    with pytest.raises(ValueError):
        push.checks_of([{"run": "true", "purpose": ""}])
    # A harness entry is only a full-hex, exact check-plan harness command.
    for cmd in ("true", HARNESS_PLAN.replace("harness --def", "candidate --def"),
                HARNESS_PLAN.replace("a" * 40, "a" * 12), HARNESS_PLAN.replace("checks/harness.json", "/abs.json"),
                HARNESS_PLAN + " --extra", HARNESS_PLAN.replace(" --definition-path checks/harness.json", ""),
                HARNESS_PLAN + " --purpose harness"):
        assert any("purpose harness check must be" in b for b in push.check_problems([{"run": cmd, "purpose": "harness"}])), cmd
    assert push.check_problems([{"run": HARNESS_PLAN.replace("--definition-commit ", "--definition-commit="),
                                 "purpose": "harness"}]) == []
    at_head = HARNESS_PLAN.replace("--definition-commit " + "a" * 40, "--definition-at-head")
    assert push.check_problems([{"run": at_head, "purpose": "harness"}]) == []
    for cmd in (at_head + " --definition-commit " + "a" * 40, at_head.replace("-at-head", "-at-head=yes"),
                at_head.replace(" --definition-path checks/harness.json", ""),
                at_head.replace("--definition-at-head", "--definition-at-head --definition-at-head")):
        assert any("purpose harness check must be" in b for b in push.check_problems([{"run": cmd, "purpose": "harness"}])), cmd


def test_ttp_checks_purpose_harness_never_counts_as_candidate_evidence(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, prguard, push
    from ttp import daemon as daemon_mod
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    selector = ("sh -c 'echo \"mandatory check selector: no case for HEAD $(git rev-parse HEAD)\"; "
                "echo gate >> " + str(log) + "; exit 1'")
    p.set_config("delivery.push_checks", [{"run": selector, "purpose": "candidate"},
                                          {"run": HARNESS_PLAN, "purpose": "harness"}, f"echo consumer >> {log}"])
    cli.main(["checks", "--purpose", "harness", "--", f"echo extra >> {log}"])
    out = capsys.readouterr().out
    assert log.read_text().split() == ["plan", "consumer", "extra"], "a candidate gate ran or a consumer was dropped"
    assert f"not run: candidate publication gate: {selector}" in out and "harness qualification only" in out
    rec = json.loads((run / "checks.json").read_text())
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rec["passed"] is True and rec["purpose"] == "harness" and rec["head"] == head
    assert rec["not_run"] == [f"not run: candidate publication gate: {selector}"]
    assert selector not in rec["commands"]
    assert not (run / "check_failure.json").exists()
    # The PR gate, the on_pass head, `ttp push --own`'s reuse and the task's other runs ignore it.
    assert "harness qualification" in prguard._checks_problem(str(repo))
    assert daemon_mod._checked_head(run, {"kind": "code", "branch": ""}) == ""
    assert push.recorded_checks(run) is None
    monkeypatch.setenv("TTP_TASK", "1")
    assert prguard._task_pass(head, str(repo)) is None
    # The candidate default runs every entry but harness-tagged ones: no harness pass is reused, the gate fails it.
    log.unlink()
    with pytest.raises(SystemExit) as exc:
        cli.main(["checks"])
    assert exc.value.code == 1 and log.read_text().split() == ["gate"]
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["purpose"] == "candidate"
    assert json.loads((run / "check_failure.json").read_text())["kind"] == "selector_missing"
    # A record from before purposes existed still counts as candidate evidence.
    (run / "checks.json").write_text(json.dumps({"head": head, "worktree": str(repo), "passed": True,
                                                 "commands": ["true"], "skipped": [], "ts": 0}))
    assert prguard._checks_problem(str(repo)) is None and push.recorded_checks(run)["head"] == head


@pytest.mark.parametrize("repo_kind", ["other_repo", "harness_repo"])
def test_ttp_checks_candidate_skips_harness_checks(env, tmp_path, monkeypatch, capsys, repo_kind):
    """The candidate `ttp checks` (a code task's checks) never runs a `purpose: harness` entry, whether
    or not the worktree's repo has its definition commit: it passes on the repo's own checks and
    reports the harness check skipped, never passed. `--purpose harness` still runs it, and with no
    other check nothing applies."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    plan = HARNESS_PLAN if repo_kind == "other_repo" else HARNESS_PLAN.replace(
        "a" * 40, subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    p.set_config("delivery.push_checks", [{"run": plan, "purpose": "harness"}, f"echo consumer >> {log}"])
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert log.read_text().split() == ["consumer"], "a candidate run ran the harness check"
    assert push.HARNESS_ONLY in out and "1 check(s) passed" in out and "1 skipped" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["purpose"] == "candidate" and rec["commands"] == [f"echo consumer >> {log}"]
    assert len(rec["skipped"]) == 1 and plan in rec["skipped"][0]
    log.unlink()
    cli.main(["checks", "--purpose", "harness"])
    assert log.read_text().split() == ["plan", "consumer"], "a harness qualification skipped its check"
    assert json.loads((run / "checks.json").read_text())["purpose"] == "harness"
    p.set_config("delivery.push_checks", [{"run": plan, "purpose": "harness"}])
    with pytest.raises(SystemExit) as exc:
        cli.main(["checks"])
    assert exc.value.code == 1
    assert json.loads((run / "checks.json").read_text())["passed"] is False


def test_candidate_harness_skip_never_hides_an_outside_scope_change(env, tmp_path):
    """A candidate run that leaves out a harness check still sees a change outside every other check's
    `if_changed` scope as outside_scope, and a run of only harness checks as nothing applying."""
    from ttp import push
    harness, scoped = push.check_list([{"run": HARNESS_PLAN, "purpose": "harness"},
                                       {"run": "true", "if_changed": "src"}])
    out = push.Skipped(push.skipped_line(scoped, "x"))
    out.scoped = True
    left = push.Skipped(push.skipped_line(harness, push.HARNESS_ONLY))
    left.harness_only = True
    assert push.outside_scope([harness, scoped], [left, out])
    assert not push.outside_scope([harness], [left])


def test_push_own_skips_harness_checks_but_push_runs_them(env, monkeypatch, tmp_path):
    from ttp import push
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path / "run"))
    _git_out(repo, "checkout", "-q", "-b", "own")
    _commit(repo, "x.txt", "x\n")
    head = _git_out(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "y.txt", "y\n")
    for plan in (HARNESS_PLAN, HARNESS_PLAN.replace("a" * 40, head)):   # another repo's plan, then this repo's
        checks = push.check_list([{"run": plan, "purpose": "harness"}, f"echo consumer >> {log}"])
        said = []
        assert push.publish(repo, "origin", "own", checks, say=said.append) == 0
        assert log.read_text().split() == ["consumer"], said
        assert any(push.HARNESS_ONLY in m for m in said)
        log.unlink()
        _commit(repo, "z.txt", plan)


def test_ttp_checks_purpose_harness_falls_back_to_the_default_plan(env, tmp_path, monkeypatch, capsys):
    """With no purpose harness check configured, `ttp checks --purpose harness` runs the built-in plan:
    a whitespace check and pytest of the harness's own test folders it has (a missing one is skipped,
    never passed). A configured purpose harness check replaces it."""
    from ttp import cli, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (repo / "runtime" / "tests").mkdir(parents=True)
    (repo / "runtime" / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run([*git, "add", "runtime"], check=True)
    subprocess.run([*git, "commit", "-qm", "tests"], check=True)
    parent = subprocess.run([*git, "rev-parse", "HEAD^"], capture_output=True, text=True).stdout.strip()
    cli.main(["checks", "--purpose", "harness"])   # a clean harness repo: exits 0
    out = capsys.readouterr().out
    assert "running the default harness plan" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["purpose"] == "harness"
    assert rec["commands"] == [push.whitespace_check(parent), "python3 -m pytest -q runtime/tests"]
    assert len(rec["skipped"]) == 1 and "bin/tests" in rec["skipped"][0]
    # A root commit is compared with the empty tree; with no test folder only the whitespace check runs.
    assert str(push.default_harness_checks(repo, "")[0]) == push.whitespace_check(parent)
    subprocess.run([*git, "checkout", "-q", "--orphan", "fresh"], check=True)
    subprocess.run([*git, "rm", "-rq", "--cached", "runtime"], check=True)
    (repo / "runtime" / "tests" / "test_ok.py").unlink()
    subprocess.run([*git, "commit", "-qm", "root"], check=True)
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["commands"] == [push.whitespace_check(push.EMPTY_TREE)]
    assert len(rec["skipped"]) == 2
    capsys.readouterr()
    # A configured purpose harness check wins: the default plan does not run.
    log = tmp_path / "log"
    _fake_check_plan_ttp(tmp_path, monkeypatch, log)
    p.set_config("delivery.push_checks", [{"run": HARNESS_PLAN, "purpose": "harness"}])
    cli.main(["checks", "--purpose", "harness", "--fresh"])
    assert "default harness plan" not in capsys.readouterr().out
    assert log.read_text().split() == ["plan"]
    assert json.loads((run / "checks.json").read_text())["commands"] == [HARNESS_PLAN]
    # The candidate purpose never gets the default plan.
    p.set_config("delivery.push_checks", [])
    cli.main(["checks", "--", "true"])
    assert json.loads((run / "checks.json").read_text())["commands"] == ["true"]


def test_a_continuation_checks_only_the_commits_after_its_inherited_marker(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push, verified
    p, repo, run, git, counts = _verified_checks_repo(env, tmp_path, monkeypatch)
    first = p.db.add_task("first hop", kind="code")
    nxt = p.db.add_task("next hop", kind="code", labels=[f"continues:{first}"])
    monkeypatch.setenv("TTP_TASK", str(first))
    marked = _commit_in(git, repo, "src/a.py", "a = 1\n")
    cli.main(["checks"])
    assert counts() == (1, 1)
    assert verified.own(p.db, first, "checks")["sha"] == marked
    # The next hop inherits the marker: its commit touches nothing under src/**, so the scoped check is
    # verified through the marker; the unscoped one covers the whole tree and runs again.
    monkeypatch.setenv("TTP_TASK", str(nxt))
    monkeypatch.setenv("TTP_RUN_ID", "42")
    head = _commit_in(git, repo, "notes.txt", "notes\n")
    capsys.readouterr()
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert counts() == (2, 1), out
    assert f"verified through {marked[:12]} (#{first}'s checks)" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["verified_through"] == marked and len(rec["verified"]) == 1
    # Its own marker carries the verified check on, so the hop after it can skip it too.
    assert len(verified.own(p.db, nxt, "checks")["commands"]) == 2
    # The push gate never reads the marker: the scoped check still applies to the pushed head.
    todo, _ = push.applicable(repo, head, push.check_list(p.config()["delivery"]["push_checks"]),
                              lambda m: None, "origin/proj")
    assert len(todo) == 2
    # A rerun on the same head by the same task (e.g. the daemon's pre-started checks) reuses what is
    # known: the scoped check through its own marker at HEAD, the unscoped one through its recorded pass.
    capsys.readouterr()
    cli.main(["checks"])
    out = capsys.readouterr().out
    assert counts() == (2, 1), out
    assert "(recorded)" in out
    # --fresh runs every check again.
    cli.main(["checks", "--fresh"])
    assert counts() == (3, 2)
    # The first hop's own rerun on its head still hits its recorded pass of every check.
    monkeypatch.setenv("TTP_TASK", str(first))
    subprocess.run([*git, "checkout", "-q", marked], check=True)
    cli.main(["checks"])
    assert counts() == (3, 2)
    subprocess.run([*git, "checkout", "-q", "-"], check=True)
    monkeypatch.setenv("TTP_TASK", str(nxt))
    # A commit after the marker under src/** runs the scoped check again.
    _commit_in(git, repo, "src/b.py", "b = 1\n")
    cli.main(["checks"])
    assert counts() == (4, 3)


def test_a_review_only_verdict_does_not_stand_in_for_a_review_that_pushes(env, monkeypatch):
    """A review-only review (kept off, delivered as a PR, no push allowed) accepts a head without
    pushing it. In a project that pushes, a later code task or review of that head still gets its
    review, so the change is delivered; a later review-only review may reuse the verdict."""
    from ttp import coordinator as coord, push, pushq, reviewreuse
    s = _pq(env, monkeypatch)
    fix, _, _, head = _re_plan(s, f"pr_branch:{s.branch}")
    d, started = _reuse_daemon(s.p, monkeypatch)
    key = reviewreuse.check_key(d.cfg)
    ro = s.p.db.add_task("Review only", "Review only: do not push.", kind="review", origin="coordinator",
                         labels=[push.REVIEW_ONLY_LABEL, reviewreuse.head_label(head, key)])
    s.p.db.update_task(ro, status="done", result=json.dumps({"status": "done", "summary": "ok, review only"}))
    assert not reviewreuse.delivered(s.p, head)
    # The code task that ends on that head gets a review, not the review-only verdict.
    done = _finish_fix(env, s.p, fix)
    assert "already accepted" not in done["text"]
    queued = s.p.db.q("SELECT id, title FROM tasks WHERE kind='review' AND status='queued'")
    assert [r["title"][:len(f"Review #{fix}")] for r in queued] == [f"Review #{fix}"]
    s.p.db.update_task(queued[0]["id"], status="cancelled")
    # The coordinator may add one, and a review that would push runs at dispatch.
    add = {"type": "task_add", "title": f"Review #{fix} by hand", "kind": "review", "spec": "check it",
           "depends_on": [fix]}
    assert coord.apply(s.p, [add]) == []
    s.p.db.update_task(s.p.db.one("SELECT id FROM tasks WHERE title=?", (add["title"],))["id"], status="cancelled")
    runs = _reuse_review(s.p, fix)
    d.dispatch()
    assert started == [runs] and s.p.db.task(runs)["status"] != "done"
    # A later review-only review of the head may take the review-only verdict.
    only = s.p.db.add_task("Review only again", "Review only.", kind="review", tier="light", origin="coordinator",
                           provider="claude", budget_usd=8.0, depends_on=[fix], labels=[push.REVIEW_ONLY_LABEL])
    d.dispatch()
    assert started == [runs] and json.loads(s.p.db.task(only)["result"])["reused_review"] == ro
    # Once the head is delivered, its verdict covers a review that would push.
    s.p.db.update_task(runs, status="cancelled")
    s.p.db.x("INSERT INTO push_queue(task,run,branch,head,target,status,created,updated) "
             "VALUES(?,1,'b',?,'origin/proj','pushed',0,0)", (ro, head))
    after = _reuse_review(s.p, fix, "Review it after the push")
    d.dispatch()
    assert started == [runs] and s.p.db.task(after)["status"] == "done"
    # A cancelled row does not deliver it; the push target's tip containing it does.
    s.p.db.x("UPDATE push_queue SET status='cancelled'")
    assert not reviewreuse.delivered(s.p, head)
    remote, branch = pushq.target(s.p)
    _git_out(s.p.root, "update-ref", f"refs/remotes/{remote}/{branch}", head)
    assert reviewreuse.delivered(s.p, head)


def test_push_result_line_matches_the_documented_format_and_pushed_flag_exit_codes(tmp_path, capsys):
    import json
    from pathlib import Path
    from ttp import push
    root = Path(__file__).resolve().parents[1] / "plugins/tt-project/template/prompts"
    for f in ("kind-code.md", "kind-review.md"):
        assert "ttp push: pushed <sha> to <branch>" in (root / f).read_text()
    marker = tmp_path / "m.json"
    marker.write_text(json.dumps({"status": "pushed", "sha": "abc123", "target": "b"}))
    assert push.result(marker, pushed_only=True) == 0
    assert capsys.readouterr().out.startswith("ttp push: pushed abc123 to b")
    marker.write_text(json.dumps({"status": "failed", "exit": 4, "log": "/nonexistent"}))
    assert push.result(marker, pushed_only=True) == push.NOT_PUSHED
    assert push.result(marker) == 0


def _fast_repo(tmp_path, files):
    import subprocess as sp
    g = lambda *a: sp.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    g("add", "-A"); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD")
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(body)
    g("add", "-A"); g("commit", "-qm", "change")
    return base, g("rev-parse", "HEAD")


def test_checks_fast_path_only_for_docs_and_data_diffs(tmp_path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "data/x/y.csv": b"1,2\n"})
    assert push.fast_path(tmp_path, base, head, push.fast_globs({"delivery": {"checks_fast_globs": DOCS_GLOBS}}))
    assert not push.fast_path(tmp_path, base, head, push.fast_globs({"delivery": {"checks_fast_globs": []}}))
    assert not push.fast_path(tmp_path, base, head, [])
    assert not push.fast_path(tmp_path, "", head, DOCS_GLOBS)
    assert push.fast_globs({"delivery": {"checks_fast_globs": ["*.rst"]}}) == ["*.rst"]


@pytest.mark.parametrize("path", [
    "CMakeLists.txt", "sub/CMakeLists.txt", "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "plugins/x/skills/y/SKILL.md", "skills/y/notes.md", "plugins/x/prompts/p.md", "AGENTS.md", "CLAUDE.md",
    "docs/conf.py", "data/gen.py", "a.sh", "m.cmake", "Makefile", "c.cpp", "h.hpp", "x.js", "x.ts", "p.json",
    "p.toml", "p.yaml", "p.yml", "p.cfg", "p.ini",
    "data/gen.rs", "data/main.go", "data/run.pl", "data/x.sql", "data/Dockerfile", "docs/build.bash",
    "tests/data/golden.txt", "test/fixture.md", "plugins/x/template/CHARTER.md", "plugins/x/templates/a.txt"])
def test_checks_fast_path_never_takes_code_build_or_installed_content(tmp_path, path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {path: b"x\n"})
    assert not push.fast_path(tmp_path, base, head, DOCS_GLOBS)
    if not path.startswith(("data/", "docs/build")):   # these are kept out by the extension allowlist alone
        assert not push.fast_path(tmp_path, base, head, [*DOCS_GLOBS, "*"])
    assert push.fast_path(tmp_path, base, head, ["*"], exclude=[])


def test_checks_fast_path_is_off_without_configured_globs(tmp_path):
    from ttp import push
    assert push.FAST_GLOBS == [] and push.fast_globs(None) == [] and push.fast_globs({"delivery": {}}) == []
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "plugins/x/README.md": b"hi\n", "docs/n.rst": b"x\n"})
    assert not push.fast_path(tmp_path, base, head, push.fast_globs(None))


def test_checks_fast_path_falls_back_for_code_manifest_or_script(tmp_path):
    from ttp import push
    base, head = _fast_repo(tmp_path, {"README.md": b"hi\n", "tests/test_a.py": b"x\n"})
    assert not push.fast_path(tmp_path, base, head, DOCS_GLOBS)


def test_checks_fast_checks_flag_crlf_and_trailing_whitespace(tmp_path):
    import subprocess as sp
    from ttp import push
    for body, rc in ((b"ok\n", 0), (b"crlf\r\n", 1), (b"trail \n", 1)):
        d = tmp_path / str(rc) / str(len(body)); d.mkdir(parents=True)
        base, head = _fast_repo(d, {"n.md": body})
        rcs = [sp.run(c, shell=True, cwd=d, executable="/bin/bash", capture_output=True).returncode
               for c in push.fast_checks(base)]
        assert (max(rcs) != 0) == bool(rc), (body, rcs)


def test_push_result_pushed_flag_on_batch_markers(tmp_path, capsys):
    from ttp import push
    done = tmp_path / "b1.json"
    done.write_text(json.dumps({"kind": "batch", "id": "b1", "phase": "finished", "outcome": "failed",
                                "target": "proj", "log": "x.log"}))
    ok = tmp_path / "b2.json"
    ok.write_text(json.dumps({"kind": "batch", "id": "b2", "phase": "finished", "outcome": "pushed",
                              "pushed_sha": "a" * 40, "target": "proj", "log": "x.log"}))
    dead = tmp_path / "b3.json"
    dead.write_text(json.dumps({"kind": "batch", "id": "b3", "phase": "running", "target": "proj"}))
    assert push.result(done) == 0 and push.result(done, pushed_only=True) == push.NOT_PUSHED
    assert push.result(ok, pushed_only=True) == 0
    assert push.result(dead, pushed_only=True) == push.NOT_PUSHED


def test_venv_rebuild_cmd_keeps_declared_upper_bounds(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["mcp>=1.2,<2", "requests"]\n'
        '[dependency-groups]\ndev = ["pytest>=8,<9"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    assert "'mcp>=1.2,<2'" in cmd and "'pytest>=8,<9'" in cmd and "requests" not in cmd


def test_venv_rebuild_cmd_single_clause_bounds_and_scoped_tables(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools<70"]\n'
        "[project]\nname = \"x\"\ndependencies = [\"mcp<2\", \"mcp[cli]<=1.9\", 'anyio<5', \"yarl~=1.2\", \"requests\"]\n"
        '[project.optional-dependencies]\ngpu = ["torch<3"]\ntest = ["pytest-x<4"]\n'
        '[dependency-groups]\ndev = ["pytest>=8,<9", "ruff"]\n'
        '[tool.foo]\ndeps = ["bad<1"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    for want in ("'mcp<2'", "'mcp[cli]<=1.9'", "anyio<5", "yarl~=1.2", "pytest-x<4", "'pytest>=8,<9'", " ruff"):
        assert want in cmd, want
    for bad in ("torch", "setuptools", "bad<1", "requests"):
        assert bad not in cmd, bad


def test_venv_rebuild_cmd_hash_in_string_and_include_group(tmp_path):
    from ttp import push
    (tmp_path / "pyproject.toml").write_text(
        '[project] # main\nname = "x"\ndependencies = ["mcp<2"]\n'
        '[dependency-groups] # groups\n'
        'test = ["pytest>=8,<9"]\n'
        'dev = ["pkg @ git+https://host/r.git#subdirectory=py", "pytest>=8,<9", "ruff"]\n'
        'both = [{include-group = "test"}, "ruff"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    for want in ("'pytest>=8,<9'", " ruff", "mcp<2"):
        assert want in cmd, want
    assert " , " not in cmd and not cmd.rstrip().endswith(",")
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = [{include-group = "test"}, "ruff"]\n')
    cmd = push.venv_rebuild_cmd(tmp_path / ".venv")
    assert " ruff" in cmd and " test" not in cmd


def test_upstream_wontfix_closes_a_note_with_a_reason_and_no_release(env, monkeypatch, capsys):
    from ttp import cli, pushq, upstream
    p = make(env)
    upstream.append("demo", 4, [{"title": "upstream: nb", "spec": "y"}])
    fp = upstream.fingerprint("upstream: nb", "y")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    assert _upstream_cli(cli, "--resolve", fp, "--wontfix", "") != 0
    assert upstream.state(fp) == "open"
    assert _upstream_cli(cli, "--resolve", fp, "--wontfix", "not a plugin bug") == 0
    assert upstream.state(fp) == "wontfix" and upstream.wontfixed(fp)["note"] == "not a plugin bug"
    assert upstream.resolution(fp) is None and fp in "\n".join(upstream.listing("demo"))
    assert "open" not in " ".join(upstream.listing("demo")).split(fp, 1)[1].split()[:1]
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.code, result=json.dumps({"status": "done", "resolves": [fp]}))
    n = len(upstream._records())
    pushq._resolve_upstream(s.p, [s.review], "0.3.1")
    assert len(upstream._records()) == n and upstream.resolution(fp) is None


def test_landing_alert_clears_without_approvals_and_is_not_raised_while_paused(env, monkeypatch):
    from ttp import alerts, pushq
    from ttp.daemon import Daemon
    s = _pq(env, monkeypatch)
    monkeypatch.setattr(pushq.runner, "boot_time", lambda: None)
    _pq_plan(s, die="push")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    _pq_batch(s)
    _pq_tend(s)
    d = Daemon(s.p.base)
    later = time.time() + 5 * 3600
    s.p.db.set_kv("paused_resources", {"push": {"reason": "maintenance", "since": time.time()}})
    assert not pushq.check_landing(s.p, d.cfg, d.alert, now=later), "paused: the queue is not retrying"
    s.p.db.set_kv("paused_resources", {})
    assert pushq.check_landing(s.p, d.cfg, d.alert, now=later)
    assert alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())
    s.p.db.x("UPDATE push_queue SET status='withdrawn' WHERE status IN ('approved','batched')")
    assert not alerts.holds(s.p.db, pushq.LANDING_ALERT, time.time() - 1, time.time())


def _pq_review_run(s, push=None):
    """A running review run of `s` that handed off done with `push` (default: its branch at its head)."""
    s.p.db.update_task(s.review, status="running")
    rid = s.p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','codex','',?,"
                   "'running')", (s.review, time.time() - 60))
    run_dir = s.p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "done", "summary": "looks good",
         "push": [{"branch": s.branch, "head": s.head}] if push is None else push}))
    return s.p.db.one("SELECT * FROM runs WHERE id=?", (rid,))


def test_a_review_approved_at_run_end_asks_the_remote_before_the_transaction_never_inside(env, monkeypatch):
    """finish_run records a run's end in one write transaction. The approval's remote lookups (the
    default branch, a pr_branch the remote may lack) are asked before it opens, so the write lock never
    waits on the network (reported: 30 s holds, one REFUSAL_TIMEOUT_S ls-remote each, after a restart
    left the cache cold). Under the guard a git network command inside the transaction raises."""
    from ttp import pushq
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_TX_GUARD", "1")
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.code, labels=json.dumps(["pr_branch:feature/x"]))   # the remote has no such branch
    pushq._default_branches.clear()
    pushq._absent_seen.clear()
    asked, real = [], pushq._git

    def git(p, *args, **kw):
        if args[0] == "ls-remote":
            asked.append((args[1], p.db.conn.in_transaction))
        return real(p, *args, **kw)
    monkeypatch.setattr(pushq, "_git", git)
    Daemon(s.p.base).finish_run(_pq_review_run(s), {"rc": 0, "started": time.time() - 60, "ended": time.time()})
    assert sorted(asked) == [("--heads", False), ("--symref", False)], asked
    task = s.p.db.task(s.review)
    assert task["status"] == "pushing", (task["status"], task["result"])
    assert "feature/x was ignored" in json.dumps(_pq_events(s.p, task=s.review)), "the prefetched answer was used"


def test_the_remotes_default_branch_answer_survives_a_daemon_restart(env, monkeypatch):
    """The default-branch cache is kept in the project's kv with its expiry: a restarted daemon (empty
    process cache) reads it, also inside a transaction, where nothing asks the remote."""
    from ttp import pushq
    s = _pq(env, monkeypatch)
    subprocess.run(["git", "-C", str(s.origin), "symbolic-ref", "HEAD", "refs/heads/proj"], check=True)
    pushq._default_branches.clear()
    assert pushq._default_branch(s.p, "origin") == "proj"
    pushq._default_branches.clear()   # a restart
    monkeypatch.setattr(pushq, "_git", lambda *a, **k: pytest.fail("asked the remote again"))
    with s.p.db.tx():
        assert pushq._default_branch(s.p, "origin") == "proj"
        assert "the remote's default branch" in pushq._refusal(s.p, "origin", "proj")
    assert pushq._default_branch(s.p, "origin") == "proj"
    # Expired, a transaction reads it as unknown (refuses nothing, as an unreachable remote): never asks.
    with s.p.db.tx():
        assert pushq._default_branch(s.p, "origin", time.time() + pushq.DEFAULT_BRANCH_TTL_S + 1) is None
        assert not pushq._absent(s.p, "origin", "feature/x")
