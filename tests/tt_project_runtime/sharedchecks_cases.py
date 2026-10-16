# Shared check failure tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new shared-checks-failure tests here, not there.
from __future__ import annotations


def _pytest_out(ids, extra_failed=0, passed=40, bars=False):
    """pytest's output: `-q` (the required command) prints its last summary line bare, the default
    verbosity (`bars`) wraps it in '='; the suite's own line may follow it."""
    lines = ["..F.", "=" * 20 + " short test summary info " + "=" * 20]
    lines += [f"FAILED {i} - AssertionError: boom" for i in ids]
    summary = f"{len(ids) + extra_failed} failed, {passed} passed, 2 skipped in 3.21s (0:00:03)"
    lines.append(f"{'=' * 10} {summary} {'=' * 10}" if bars else summary)
    lines.append("ttp-suite: wall 3.2s cpu 9.1s in 3.21s")
    return "\n".join(lines) + "\n"


def _shared_fail_task(p, tmp_path, output, started=1000.0):
    """A code task waiting on its own detached checks, which failed with `output` (check_failure.json)."""
    tid, path = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc="1")
    run_dir = tmp_path / f"run-{tid}"
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (run_dir / "check_failure.json").write_text(json.dumps({"kind": "check_failed", "head": head, "rc": 1,
                                                            "output": output, "check": "pytest -q"}))
    (run_dir / "checks.pid").write_text(json.dumps({"pid": 1, "ts": started, "worktree": str(path), "head": head,
                                                    "cmd": ["pytest -q"], "fresh": False, "full": False}))
    return tid, run_dir


_IDS = ["tests/test_a.py::test_one", "tests/test_b.py::test_two[x - y]"]


def _seed_other_failures(p, ids, n=2):
    from ttp import sharedchecks
    for k in range(n):
        sharedchecks.record(p.db, 9000 + k, f"{k:040x}", ids, time.time() - 600)


def _shared_daemon(p, monkeypatch):
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, run_id, tid, argv: started.append((tid, argv)) or True)
    return d, started


def test_failing_ids_reads_pytest_summaries_and_nothing_else():
    from ttp import sharedchecks
    assert sharedchecks.failing_ids(_pytest_out(_IDS)) == (sorted(_IDS), True)
    out = _pytest_out(["tests/test_a.py::test_one"]).replace("FAILED", "ERROR").replace(" failed,", " error,")
    assert sharedchecks.failing_ids(out) == (["tests/test_a.py::test_one"], True)
    assert sharedchecks.failing_ids(_pytest_out(_IDS, bars=True)) == (sorted(_IDS), True)
    # A summary counting more failures than the lines shown (cut output): not complete.
    assert sharedchecks.failing_ids(_pytest_out(_IDS, extra_failed=1))[1] is False
    assert sharedchecks.failing_ids(_pytest_out(_IDS, extra_failed=1, bars=True))[1] is False
    # Another runner's output.
    assert sharedchecks.failing_ids("error[E0308]: mismatched types\nerror: could not compile `x`\n") == ([], False)
    assert sharedchecks.failing_ids("FAILED (failures=2)\nRan 9 tests in 0.1s\n") == ([], False)


def test_failing_ids_reads_real_pytest_q_output(tmp_path):
    from ttp import sharedchecks
    (tmp_path / "test_tiny.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_tiny.py"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")})
    assert r.returncode == 1, r.stdout + r.stderr
    assert sharedchecks.failing_ids(r.stdout) == (["test_tiny.py::test_bad"], True), r.stdout


def test_a_shared_checks_failure_holds_tasks_in_one_incident_with_one_fix_task(env, monkeypatch, tmp_path):
    from ttp import audit, sharedchecks
    p = make(env)
    d, started = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tids = [_shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0] for _ in range(2)]
    now = time.time()
    for tid in tids:
        d._probe_passed(p.db.task(tid), now)
    d._probe_passed(p.db.task(tids[0]), now)   # the same failure again: still held, not counted twice
    for tid in tids:
        task = p.db.task(tid)
        res = json.loads(task["result"])
        assert "woke" not in res and not res.get("escalated_wake"), res
        assert task["not_before"] > now + 3600 and not _ready(p, tid)
        assert res["shared_hold"]["ids"] == sorted(_IDS) and res["shared_hold"]["reruns"] == 0
    (inc,) = p.db.q("SELECT * FROM checks_incidents")
    assert sorted(json.loads(inc["tasks"])) == tids and inc["held"] == 2 and inc["closed"] is None
    (fix,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix shared check failure%'")
    assert fix["id"] == inc["fix_task"] and fix["kind"] == "code"
    assert all(i in fix["spec"] for i in _IDS) and all(f"#{t}" in fix["spec"] for t in tids)
    assert len(p.db.q("SELECT id FROM events WHERE kind='checks_shared'")) == 1
    assert not started
    assert len(p.db.q("SELECT id FROM runs WHERE task IN (?,?)", tids)) == 2   # no model run
    line = sharedchecks.audit_line(p.db, now - 60)
    assert "1 incident(s), 2 task(s) held, 2 wakes avoided" in line, line
    assert line in audit.lines(p.db, now + 1)


def test_failures_of_one_changes_own_earlier_tasks_are_not_shared(env):
    """A code task, its review fix and its `continue:` task fail the same id on successive heads:
    that is the change's own regression, so it is not shared; two tasks of other lineages make it so."""
    from ttp import sharedchecks
    p = make(env)
    now = time.time()
    code = p.db.add_task("code: change", "spec", kind="code")
    fix = p.db.add_task("Fix review #1: change", "spec", kind="code", labels=[f"continues:{code}"])
    cont = p.db.add_task("continue: change", "spec", kind="code", labels=[f"continues:{fix}"])
    review = p.db.add_task(f"Review #{code}: change", "spec", kind="review", labels=[f"auto_review:{code}"])
    rereview = p.db.add_task(f"Re-review #{review}: change", "spec", kind="review", labels=[f"auto_review:{fix}"])
    for k, t in enumerate((code, review, fix, rereview)):
        sharedchecks.record(p.db, t, f"{k // 2 + 1:040x}", _IDS, now - 600)
    assert len({r["root"] for r in p.db.q("SELECT root FROM checks_failures")}) == 1
    via_review = p.db.add_task("Fix review: change", "spec", kind="code", labels=[f"continues:{rereview}"])
    assert sharedchecks.root(p.db, via_review) == code   # a hop through a review keeps the lineage
    assert not sharedchecks.shared(p.db, cont, f"{9:040x}", _IDS, now, {})
    for k in range(2):
        other = p.db.add_task(f"code: other {k}", "spec", kind="code")
        sharedchecks.record(p.db, other, f"{k + 5:040x}", _IDS, now - 600)
    assert sharedchecks.shared(p.db, cont, f"{9:040x}", _IDS, now, {})


def test_shared_checks_count_other_lineages_not_tasks(env):
    """Two tasks of one other change (its code task and its review fix, on successive heads) are one
    lineage: not enough for shared_min_tasks=2; two other changes are."""
    from ttp import sharedchecks
    p = make(env)
    now = time.time()
    mine = p.db.add_task("code: mine", "spec", kind="code")
    other = p.db.add_task("code: other", "spec", kind="code")
    ofix = p.db.add_task("Fix review #1: other", "spec", kind="code", labels=[f"continues:{other}"])
    for k, t in enumerate((other, ofix)):
        sharedchecks.record(p.db, t, f"{k + 1:040x}", _IDS, now - 600)
    assert not sharedchecks.shared(p.db, mine, f"{9:040x}", _IDS, now, {})
    third = p.db.add_task("code: third", "spec", kind="code")
    sharedchecks.record(p.db, third, f"{5:040x}", _IDS, now - 600)
    assert sharedchecks.shared(p.db, mine, f"{9:040x}", _IDS, now, {})


def test_the_known_bad_push_tip_makes_a_failure_shared(env, monkeypatch, tmp_path):
    from ttp import pushq
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    tid, _ = _shared_fail_task(p, tmp_path, _pytest_out(_IDS[:1]))
    p.db.set_kv(pushq.KV, {"bad_tip": {"tip": "f" * 40, "fails": pushq.TIP_FAILS, "ids": _IDS[:1]}})
    d._probe_passed(p.db.task(tid), time.time())
    assert "shared_hold" in json.loads(p.db.task(tid)["result"])


@pytest.mark.parametrize("output", [
    _pytest_out(_IDS + ["tests/test_c.py::test_new"]),        # one id is not shared: mixed
    "error[E0308]: mismatched types\nerror: could not compile `x`\n",   # another runner
    _pytest_out(_IDS, extra_failed=1)])                           # the list is not complete
def test_a_checks_failure_not_fully_shared_wakes_the_task_as_before(env, monkeypatch, tmp_path, output):
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tid, _ = _shared_fail_task(p, tmp_path, output)
    d._probe_passed(p.db.task(tid), time.time())
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "its checks failed" in res["woke"] and "shared_hold" not in res
    assert _ready(p, tid)
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix shared check failure%'")


def test_a_held_task_reruns_its_checks_once_the_tip_moves_past_its_tests_or_the_backoff(env, monkeypatch, tmp_path):
    from ttp import pushq, sharedchecks
    p = make(env)
    d, started = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tid, run_dir = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))
    tips, touched = ["t1"], []
    monkeypatch.setattr(d, "_push_tip", lambda: tips[0])
    monkeypatch.setattr(pushq, "touches", lambda p_, old, new, files: touched.append((old, new, files)) or new == "t3")
    now = time.time()
    d._probe_passed(p.db.task(tid), now)
    d._probe_passed(p.db.task(tid), now + 60)           # tip unchanged: sleeps on
    tips[0] = "t2"
    d._probe_passed(p.db.task(tid), now + 120)          # tip moved, but not past the failing tests
    assert touched == [("t1", "t2", ["tests/test_a.py", "tests/test_b.py"])] and not started
    tips[0] = "t3"
    d._probe_passed(p.db.task(tid), now + 180)          # tip moved past a commit touching them
    assert len(started) == 1 and started[0][0] == tid and started[0][1] == ["pytest -q"]
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert "woke" not in res and res["shared_hold"]["reruns"] == 1 and task["not_before"] > now + 3600
    # The rerun failed the same way (a new checks.pid): held again; then the backoff alone reruns it.
    pid = json.loads((run_dir / "checks.pid").read_text())
    (run_dir / "checks.pid").write_text(json.dumps({**pid, "ts": 2000.0}))
    d._probe_passed(p.db.task(tid), now + 240)
    hold = json.loads(p.db.task(tid)["result"])["shared_hold"]
    assert hold["started"] == 2000.0 and hold["tip"] == "t3"
    d._probe_passed(p.db.task(tid), hold["until"] + 1)
    assert len(started) == 2
    # Past RERUNS_MAX reruns the next failure wakes the task at its tier.
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "shared_hold": {**res["shared_hold"],
                                                                     "reruns": sharedchecks.RERUNS_MAX}}))
    (run_dir / "checks.pid").write_text(json.dumps({**pid, "ts": 3000.0}))
    d._probe_passed(p.db.task(tid), now + 300)
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "its checks failed" in res["woke"]
    assert p.db.task(tid)["not_before"] <= now + 300   # due at the wake's own time


def test_touches_finds_commits_changing_the_failing_test_files(env):
    from ttp import pushq
    p = make(env)
    git = ["git", "-C", str(p.root)]
    old = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (p.root / "tests").mkdir(exist_ok=True)
    (p.root / "tests" / "test_a.py").write_text("def test_one(): pass\n")
    subprocess.run([*git, "add", "tests/test_a.py"], check=True, capture_output=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix test"], check=True,
                   capture_output=True)
    new = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert pushq.touches(p, old, new, ["tests/test_a.py"])
    assert not pushq.touches(p, old, new, ["tests/test_b.py"])
    assert pushq.touches(p, "0" * 40, new, ["tests/test_b.py"])   # git cannot tell: rerun
