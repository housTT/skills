# Memory-commit tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Each memory write or retirement is committed by the process that made
# it, with its own message, and never left on disk uncommitted for the harness sweep.
from __future__ import annotations


def _memory_commit_log(p, *paths):
    return subprocess.run(["git", "-C", str(p.harness), "log", "--format=%s", "--", *paths],
                          capture_output=True, text=True).stdout.splitlines()


def _committed_clean(p):
    assert _harness_dirty(p) == "", _harness_dirty(p)
    assert "sweep:" not in _daemon_log_text(p), _daemon_log_text(p)


def test_memory_writes_in_a_rolled_back_transaction_are_undone(env):
    """A transaction that rolls back after a retirement, an add or a supersede inside it leaves the
    entries as they were and the harness clean: nothing waits uncommitted for the sweep."""
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    other = p.add_memory("Another fact.", kind="fact")
    index = p.memory_index.read_text()
    for step in (lambda: p.forget_memory(old.stem, why="done"),
                 lambda: p.add_memory("A fact the rollback takes back.", kind="fact"),
                 lambda: p.supersede_memory("A merged fact.", [other.stem], kind="fact"),
                 lambda: (p.forget_memory(old.stem, why="done"), p.add_memory("Then a new fact.", kind="fact"))):
        with pytest.raises(RuntimeError):
            with p.db.tx():
                step()
                raise RuntimeError("a later write of the transaction failed")
        assert old.exists() and other.exists() and not list((p.memory_dir / "archive").glob("*"))
        assert p.memory_index.read_text() == index
        _committed_clean(p)
    # A line another process added meanwhile stays; only this transaction's change is undone.
    with pytest.raises(RuntimeError):
        with p.db.tx():
            p.forget_memory(old.stem, why="done")
            from ttp.project import durable_append
            durable_append(p.memory_index, "- [elsewhere](memory/fact-elsewhere.md) (fact)\n")
            raise RuntimeError("x")
    assert old.exists() and f"](memory/{old.name})" in p.memory_index.read_text()
    assert "fact-elsewhere.md" in p.memory_index.read_text()


def test_coordinator_memory_actions_in_the_run_end_transaction_commit_each_with_no_sweep(env):
    """The daemon applies a coordinator turn inside the run's end transaction (finish_run): the add
    and the retirement it supersedes each get their own commit once it commits, and the first
    commit's sweep never takes the retirement's files (os.replace keeps their old mtime)."""
    p = make(env)
    from ttp import coordinator as coord
    old = p.add_memory("An old decision.", kind="decision")
    with p.db.tx():
        probs = coord.apply(p, [{"type": "memory_add", "text": "A new decision.", "memory_kind": "decision",
                                 "supersedes": [old.stem], "why": "replaced"}], turn=7)
        assert probs == []
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    assert any(s.startswith("memory (decision): A new decision.") for s in _memory_commit_log(p))
    # A memory_forget alone, and an expiry retirement, the same way.
    later = p.add_memory("A later fact.", kind="fact")
    with p.db.tx():
        assert coord.apply(p, [{"type": "memory_forget", "name": later.stem}], turn=8) == []
    _committed_clean(p)
    assert (p.memory_dir / "archive" / later.name).exists()


def test_a_failing_after_commit_callback_never_drops_the_commits_queued_after_it(env):
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    with pytest.raises(OSError):
        with p.db.tx():
            p.db.after_commit(lambda: (_ for _ in ()).throw(OSError("an earlier side effect failed")))
            p.forget_memory(old.stem, why="done")
    _committed_clean(p)
    assert (p.memory_dir / "archive" / old.name).exists()


def test_memory_cli_add_forget_and_expiry_leave_the_harness_clean(env):
    p = make(env)
    from ttp import cli, ends
    old = p.add_memory("Old news.", kind="fact")
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["memory", "demo", "A new fact.", "--forget", old.stem])
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    temp = p.add_memory("A temporary fact.", kind="fact", end={"expires": time.time() + 60})
    assert ends.Ends(p).tick(time.time() + 120)
    _committed_clean(p)
    assert (p.memory_dir / "archive" / temp.name).exists()
    assert _memory_commit_log(p, f"memory/archive/{temp.name}")[0].startswith(f"memory retired: {temp.stem}")


@pytest.mark.parametrize("how", ["exit", "kill"])
def test_a_process_ending_inside_the_transaction_leaves_no_uncommitted_retirement(env, how):
    """sys.exit inside the transaction rolls it back and puts the entry back. A process killed there
    (os._exit: no undo runs) leaves the files moved; the replayed turn's retirement commits them."""
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    end = "sys.exit(3)" if how == "exit" else "os._exit(3)"
    code = (f"import os, sys\nfrom ttp.project import Project\np = Project({str(p.base)!r})\n"
            f"with p.db.tx():\n    p.forget_memory({old.stem!r}, why='done')\n    {end}\n")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(RUNTIME)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, r.stderr
    if how == "exit":
        assert old.exists() and f"](memory/{old.name})" in p.memory_index.read_text()
        _committed_clean(p)
        return
    assert not old.exists() and old.name in _harness_dirty(p)
    p.forget_memory(old.stem, why="done")   # the replay finds it archived and commits what was left
    _committed_clean(p)
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    # A replay of a retirement already committed makes no commit.
    n = len(_memory_commit_log(p))
    p.forget_memory(old.stem, why="done")
    assert len(_memory_commit_log(p)) == n


def test_a_replayed_coordinator_forget_runs_no_git_under_the_write_lock(env, monkeypatch):
    """A coordinator memory_forget of an entry already archived (a replayed turn) inside the run-end
    transaction checks whether a killed run left it uncommitted only once the transaction ends:
    git never runs while the write lock is held. It commits what was left, and a replay of a
    retirement already committed makes no commit."""
    p = make(env)
    from ttp import coordinator as coord
    old = p.add_memory("An old decision.", kind="decision")
    code = (f"import os\nfrom ttp.project import Project\np = Project({str(p.base)!r})\n"
            f"with p.db.tx():\n    p.forget_memory({old.stem!r}, why='done')\n    os._exit(3)\n")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(RUNTIME)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3, r.stderr
    assert not old.exists() and old.name in _harness_dirty(p)
    real, locked = subprocess.run, []

    def spy(cmd, *a, **kw):
        if cmd and cmd[0] == "git" and p.db.conn.in_transaction:
            locked.append(cmd)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    for _ in range(2):   # the replay that commits the leftovers, then one with nothing left
        n = len(_memory_commit_log(p))
        with p.db.tx():
            assert coord.apply(p, [{"type": "memory_forget", "name": old.stem}], turn=9) == []
        assert locked == []
        monkeypatch.setattr(subprocess, "run", real)
        _committed_clean(p)
        monkeypatch.setattr(subprocess, "run", spy)
    assert len(_memory_commit_log(p)) == n
    assert _memory_commit_log(p, f"memory/archive/{old.name}")[0].startswith(f"memory retired: {old.stem}")
    # Retiring a live entry and replaying an archived one in one transaction: no git under the lock either.
    fresh = p.add_memory("A fresh fact.", kind="fact")
    with p.db.tx():
        assert coord.apply(p, [{"type": "memory_forget", "name": old.stem},
                               {"type": "memory_forget", "name": fresh.stem}], turn=10) == []
    assert locked == []
    monkeypatch.setattr(subprocess, "run", real)
    _committed_clean(p)
    assert (p.memory_dir / "archive" / fresh.name).exists()


def test_an_undo_that_raises_on_rollback_is_logged_and_the_next_still_runs(env):
    p = make(env)
    ran = []

    def bad():
        raise OSError("disk gone")
    with pytest.raises(RuntimeError):
        with p.db.tx():
            p.db.on_rollback(lambda: ran.append(1))
            p.db.on_rollback(bad)
            raise RuntimeError("x")
    assert ran == [1]
    assert "rollback undo" in _daemon_log_text(p) and "disk gone" in _daemon_log_text(p)


def test_memory_cli_dry_run_writes_retires_and_commits_nothing(env):
    """--dry-run on an add, a --supersedes merge and a --forget only says what it would add and retire,
    marked as a dry run: the memory files, index and harness HEAD stay as they were."""
    from ttp import cli
    p = make(env)
    old = p.add_memory("An old decision.", kind="decision")
    other = p.add_memory("Another fact.", kind="fact")
    head = lambda: subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout
    snapshot = lambda: sorted((str(f.relative_to(p.memory_dir)), f.read_text())
                              for f in p.memory_dir.rglob("*") if f.is_file())
    before, index, h0 = snapshot(), p.memory_index.read_text(), head()
    for argv, said in ((["A merged fact.", "--title", "x", "--supersedes", other.stem],
                       ["would add", "fact-x.md", f"would retire to {p.memory_dir / 'archive' / other.name}"]),
                      (["--forget", old.stem, other.stem],
                       [f"archive/{old.name}", f"archive/{other.name}"]),
                      (["A new fact."], ["would add", "fact-a-new-fact.md"])):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["memory", "demo", *argv, "--dry-run"])
        lines = out.getvalue().splitlines()
        assert lines and all(x.startswith("dry run, nothing written or committed: ") for x in lines), lines
        assert all(s in out.getvalue() for s in said), out.getvalue()
        # An add or merge still shows the pinned worker-memory total (as it stands); a --forget alone does not.
        assert ("pinned worker memory: " in out.getvalue()) == ("--forget" not in argv), out.getvalue()
        assert snapshot() == before and p.memory_index.read_text() == index, f"{argv} wrote"
        assert head() == h0 and _harness_dirty(p) == "", f"{argv} committed or left changes"
    # A refused name is still refused in a dry run.
    with pytest.raises(SystemExit):
        cli.main(["memory", "demo", "A merged fact.", "--supersedes", "no-such-entry", "--dry-run"])
    assert snapshot() == before and head() == h0
    # Over its room, a dry-run merge still warns, marked as a dry run, and still writes nothing.
    big = [p.add_memory(f"RULE-{i:02} " + "r" * 900, kind="restriction") for i in range(30)]
    before, index, h0 = snapshot(), p.memory_index.read_text(), head()
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
        cli.main(["memory", "demo", "RULE-00 and RULE-01 merged.", "--kind", "restriction",
                  "--supersedes", big[0].stem, big[1].stem, "--dry-run"])
    assert all(x.startswith("dry run, nothing written or committed: ") for x in out.getvalue().splitlines())
    assert err.getvalue().startswith("dry run, nothing written or committed: warning: pinned worker memory "
                                     "over budget: "), err.getvalue()
    assert snapshot() == before and p.memory_index.read_text() == index and head() == h0
    assert _harness_dirty(p) == ""
