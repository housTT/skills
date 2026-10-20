# Project-root HEAD guard tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new root HEAD guard tests here, not there.
from __future__ import annotations


def _roothead_setup(env):
    p = make(env)
    from ttp import daemon as dm
    from ttp import worktree
    repo = env["repo"]
    home = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    d = dm.Daemon(p.base)
    d.guard_root_head()   # sees the root on its branch: recorded as where it belongs
    assert worktree.root_branch(p) == home
    return p, d, dm, worktree, repo, home


def _roothead_commit(repo, msg):
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)
    return _git_out(repo, "rev-parse", "HEAD")


def test_a_root_left_detached_is_put_back_before_a_worktree_is_branched_and_its_commits_kept(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    tip = _git_out(repo, "rev-parse", "HEAD")
    # A task ran `git checkout --detach` in the root and committed there.
    _git_out(repo, "checkout", "-q", "--detach", tip)
    stray = _roothead_commit(repo, "made while detached")
    t = p.db.add_task("needs a worktree", "s", kind="code", tier="light", origin="user")
    path, _ = d._workdir_for(p.db.task(t))
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == home
    assert _git_out(repo, "rev-parse", home) == tip, "the branch is not moved"
    assert _git_out(repo, "rev-parse", f"rescue/root-guard-{stray[:12]}") == stray, "the detached commits are kept"
    assert subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", stray, "HEAD"]).returncode == 1, \
        "the worktree does not start from the stray HEAD"
    got = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'root-head-restored:%'")
    assert len(got) == 1 and got[0]["severity"] == "low" and home in got[0]["text"]
    assert not p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='root-head'")
    assert not p.db.kv(dm.ROOT_HEAD_KEY)


def test_a_root_with_local_changes_is_never_touched_alerted_once_and_cleared_when_back(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "--detach", tip)
    stray = _roothead_commit(repo, "made while detached")
    (repo / "README.md").write_text("edited while detached\n")
    alerts = lambda: p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='root-head'")
    for n in range(2):
        t = p.db.add_task(f"needs a worktree {n}", "s", kind="code", tier="light", origin="user")
        path, _ = d._workdir_for(p.db.task(t))
        assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "left as it is"
        assert _git_out(repo, "rev-parse", "HEAD") == stray
        assert (repo / "README.md").read_text() == "edited while detached\n", "nothing discarded"
        assert subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", stray, "HEAD"]).returncode == 1, \
            "the worktree does not start from the stray HEAD"
    got = alerts()
    assert len(got) == 1, "one alert per condition"
    assert "uncommitted changes" in got[0]["text"] and "README.md" in got[0]["text"] and home in got[0]["text"]
    assert p.db.kv(dm.ROOT_HEAD_KEY)["at"] == stray
    assert worktree.root_branch(p) == home, "a moved root is never adopted as its branch"
    # Fixed by hand: the sweep clears it.
    _git_out(repo, "checkout", "-q", "README.md")
    _git_out(repo, "checkout", "-q", home)
    d._root_head_checked = 0.0
    d.guard_root_head()
    assert not p.db.kv(dm.ROOT_HEAD_KEY)
    assert p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='root-head'")


def test_restore_root_refuses_uncommitted_tracked_changes(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    _git_out(repo, "checkout", "-q", "-b", "elsewhere")
    (repo / "README.md").write_text("edited elsewhere\n")
    ok, why = worktree.restore_root(repo, home, 7)
    assert not ok and "uncommitted changes" in why and "README.md" in why
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == "elsewhere"
    assert (repo / "README.md").read_text() == "edited elsewhere\n"
    _git_out(repo, "checkout", "-q", "README.md")
    assert worktree.restore_root(repo, home, 7) == (True, "")
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == home


def test_worktree_base_is_never_a_detached_or_moved_root_head(env):
    p, d, dm, worktree, repo, home = _roothead_setup(env)
    if worktree.named_base(p):
        p.set_config("delivery.base_ref", "")
    assert not worktree.named_base(p), "this test is about the checked-out-branch fallback"
    tip = _git_out(repo, "rev-parse", "HEAD")
    _git_out(repo, "checkout", "-q", "--detach", tip)
    assert worktree.base_ref(p) == home and worktree.resolve_base(p) == home
    # Nothing known: refused with the reason, not branched from HEAD.
    p.db.set_kv(worktree.ROOT_HOME_KEY, None)
    assert worktree.base_ref(p) == ""
    with pytest.raises(RuntimeError, match="detached HEAD"):
        worktree.resolve_base(p)
    # On another branch than worktree.root_branch: that branch, not the one checked out.
    _git_out(repo, "checkout", "-q", "-b", "moved")
    p.set_config("worktree.root_branch", home)
    assert worktree.base_ref(p) == home
    # A user's own switch, with no root alert open and no configured branch, is adopted.
    p.set_config("worktree.root_branch", "")
    d.guard_root_head()
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == "moved"
    assert worktree.root_branch(p) == "moved" and worktree.base_ref(p) == "moved"
    assert not p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'root-head%'")
