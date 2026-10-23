# Driver progress report tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new `ttp progress report` and progress and stall check
# (progress.check) tests here, not there.
from __future__ import annotations


def _report(*args):
    """`ttp progress report ...` in process: its exit code (0 when it returns)."""
    from ttp import cli
    try:
        cli.main(["progress", "report", *args])
    except SystemExit as e:
        return e.code or 0
    return 0


def _lines(p):
    return [json.loads(ln) for ln in (p.state / "progress.jsonl").read_text().splitlines()]


def _driver_reports(p, pid, kinds, key="filler", t0=None):
    from ttp import progress
    from ttp.runner import boot_id
    t0 = time.time() - 600 if t0 is None else t0
    for i, kind in enumerate(kinds):
        progress.append(p.state, {"ts": t0 + i, "key": key, "kind": kind, "reason": f"lock busy (exit 75) #{i}",
                                  "source": f"pid {pid}", "pid": pid, "boot": boot_id(), "job": None})


def test_a_progress_report_appends_one_well_formed_line(env, monkeypatch):
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", "12")
    assert _report("refusal", "--key", "filler", "lock busy\n  (exit 75)") == 0
    assert _report("result", "--key", "filler", "batch 3 done") == 0
    first, second = _lines(p)
    assert {"ts", "key", "kind", "reason", "source"} <= set(first), first
    assert (first["key"], first["kind"], first["reason"]) == ("filler", "refusal", "lock busy (exit 75)")
    assert first["pid"] == os.getppid() and "task 12" in first["source"] and first["job"] is None, first
    assert second["kind"] == "result" and second["ts"] >= first["ts"]
    # A bad kind or no key is a usage error that writes nothing; inside a `ttp detach` job the key
    # defaults to its task and name.
    assert _report("progress", "--key", "filler", "x") == 2
    assert _report("refusal", "no key given") == 2
    monkeypatch.setenv("TTP_DETACH_RC", str(env["tmp"] / "run" / "nightly.rc"))
    assert _report("failure", "step 3 exit 4") == 0
    last = _lines(p)[-1]
    assert len(_lines(p)) == 3 and last["key"] == "12/nightly" and last["job"].endswith("nightly.rc"), last
    assert "job nightly" in last["source"], last


def test_a_progress_report_needs_no_daemon_and_exits_0_even_when_it_cannot_write(env, monkeypatch, capsys):
    p = make(env)
    from ttp import progress
    from ttp.cli import daemon_state
    assert daemon_state(p).startswith("NOT RUNNING")
    run_env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base)}
    r = subprocess.run([sys.executable, "-m", "ttp", "progress", "report", "failure", "--key", "recheck",
                        "probe exit 4"], env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert [(x["key"], x["kind"]) for x in _lines(p)] == [("recheck", "failure")]
    # A state folder that cannot take the line: said on stderr, still exit 0, so a `set -e` driver goes on.
    monkeypatch.setenv("TTP_PROJECT", str(p.base))

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(progress, "append", full)
    assert _report("refusal", "--key", "recheck", "busy") == 0
    assert "not recorded" in capsys.readouterr().err


def test_concurrent_progress_reports_never_interleave(env):
    p = make(env)
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from ttp import progress\n"
              "for i in range(40):\n"
              "    progress.append(sys.argv[2], {'ts': i, 'key': sys.argv[3], 'kind': 'refusal', 'reason': 'x' * 500})\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(RUNTIME), str(p.state), f"k{n}"]) for n in range(6)]
    assert [pr.wait(timeout=120) for pr in procs] == [0] * 6
    from ttp import progress
    raw = (p.state / "progress.jsonl").read_text().splitlines()
    assert all(json.loads(ln)["reason"] == "x" * 500 for ln in raw)   # every line whole
    got = progress.entries(p.state)
    assert len(raw) == len(got) == 240
    assert sorted({r["key"] for r in got}) == [f"k{n}" for n in range(6)]


def test_the_progress_file_rotates_and_keeps_one_old_generation(env):
    p = make(env)
    from ttp import progress
    for i in range(30):
        progress.append(p.state, {"ts": i, "key": "k", "kind": "failure", "reason": "r" * 100}, max_bytes=1000)
    log, old = p.state / progress.LOG, p.state / progress.OLD
    assert old.exists() and log.stat().st_size <= 1000 and old.stat().st_size <= 1000
    assert not list(p.state.glob("progress.2*"))
    kept = progress.entries(p.state)
    assert 0 < len(kept) < 30 and [r["ts"] for r in kept] == sorted(r["ts"] for r in kept)
    assert kept[-1]["ts"] == 29


def test_status_shows_a_running_driver_with_only_refusals_as_stalled(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    driver = subprocess.Popen(["sleep", "60"])
    try:
        _driver_reports(p, driver.pid, ["result"] + ["refusal"] * 4)
        assert "stalled" not in status_text(p)   # 4 of 5: not yet
        _driver_reports(p, driver.pid, ["failure"], t0=time.time() - 60)
        line = [ln for ln in status_text(p).splitlines() if "driver filler stalled" in ln]
        assert len(line) == 1 and "4 refusals, 1 failure in a row, no result" in line[0], status_text(p)
        assert f"pid {driver.pid}" in line[0]
        assert health(p, p.db)["drivers_stalled"] == [line[0].strip()]
        _driver_reports(p, driver.pid, ["result"], t0=time.time() - 30)
        assert "stalled" not in status_text(p)   # a result ends the streak
        _driver_reports(p, driver.pid, ["refusal"] * 5, t0=time.time() - 20)
        assert "driver filler stalled" in status_text(p)
    finally:
        driver.kill()
        driver.wait()
    assert "stalled" not in status_text(p)   # its process ended: the daemon's observation covers it


def test_the_daemon_raises_one_keyed_observation_per_stalled_streak(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)

    def raised():
        return [e for e in p.db.q("SELECT * FROM events WHERE source='daemon:progress' ORDER BY id")]
    _driver_reports(p, 99999, ["refusal"] * 4, key="recheck")
    d.watch_progress(every_s=0)
    assert raised() == []
    _driver_reports(p, 99999, ["refusal"], key="recheck", t0=time.time() - 100)
    d.watch_progress(every_s=0)
    d.watch_progress(every_s=0)   # the same streak: no second observation
    ev = raised()
    assert len(ev) == 1 and "Driver recheck made no progress: 5 refusals in a row" in ev[0]["text"], ev
    assert "lock busy (exit 75) #0" in ev[0]["text"]
    _driver_reports(p, 99999, ["result"] + ["failure"] * 5, key="recheck", t0=time.time() - 50)
    d.watch_progress(every_s=0)
    ev = raised()
    assert len(ev) == 2 and "5 failures in a row" in ev[1]["text"], ev


# ---- the progress and stall check (progress.check, Daemon.check_progress) ----

def _prog_events(p, kind=None):
    rows = p.db.q("SELECT * FROM events WHERE source='daemon:progress' ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


def _prog_waiting(p, title, probe, labels=()):
    tid = p.db.add_task(title, "spec", labels=list(labels))
    p.db.update_task(tid, status="waiting", result=json.dumps({"status": "waiting", "retry_when": probe}))
    return tid


def _prog_ready(p, tid, since):
    """A queued task that a check first saw ready at `since` (its no_progress clock)."""
    from ttp import progress
    st = progress.load(p.db)
    st["ready"][str(tid)] = {"since": since}
    p.db.set_kv(progress.STATE_KEY, st)


def _prog_clear(p, cfg, now, current_probe=None):
    """Three healthy checks spanning half an hour: the default hysteresis lets a key clear."""
    from ttp import progress
    for dt in (0, 60, 1900):
        assert progress.check(p.db, cfg, p.state, now + dt, current_probe=current_probe) == []


def test_progress_no_progress_fires_past_threshold_only_while_work_is_queued(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Build the thing", "spec")
    _prog_ready(p, tid, now - 3600)
    assert progress.check(p.db, p.config(), p.state, now) == []          # 1 h: under the 2 h threshold
    _prog_ready(p, tid, now - 3 * 3600)
    got = progress.check(p.db, p.config(), p.state, now)
    assert [e["kind"] for e in got] == ["no_progress"]
    assert set(got[0]["keys"]) == {f"task:#{tid}", "project"}
    ev = _prog_events(p, "no_progress")[0]
    assert "queued, never started" in ev["text"] and f"task #{tid}" in ev["text"] and ev["status"] == "queued"
    assert progress.check(p.db, p.config(), p.state, now + 60) == []     # unchanged: no second wake
    lines, ident = progress.digest_lines(p.db, now + 60)
    assert lines[0].startswith("## Progress") and f"task:#{tid}" in ident
    # Nothing queued or running: the clock stops, the keys clear into the history once healthy long enough.
    p.db.update_task(tid, status="cancelled")
    _prog_clear(p, p.config(), now + 120)
    assert progress.digest_lines(p.db, now + 2100) == ([], "")
    assert "2 cleared" in progress.audit_line(p.db, now - 60, now + 2100)
    # A finished run resets the clock: a queued task with a recent result is not stuck.
    t2 = p.db.add_task("Another", "spec")
    _prog_ready(p, t2, now - 5 * 3600)
    p.db.x("INSERT INTO runs(role,task,started,ended,status) VALUES('worker',?,?,?,'ok')", (t2, now + 1800, now + 2000))
    assert progress.check(p.db, p.config(), p.state, now + 2200) == []
    # Thresholds are configurable; 0 turns one off.
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0.01}}}
    p.db.x("DELETE FROM runs")
    got = progress.check(p.db, cfg, p.state, now + 2300)
    assert got and got[0]["keys"] == ["project"]


def test_progress_a_key_that_drops_out_for_a_check_neither_clears_nor_wakes_again(env):
    """Review #3485 finding 1: a stuck queued task drops out whenever every worker slot is briefly
    full. It woke the coordinator again as new each time it came back (3 wakes in 6 checks)."""
    from ttp import progress
    p = make(env)
    cfg = {"budget": {"max_parallel_workers": 1}}
    now = time.time()
    tid = p.db.add_task("Stuck work", "spec")
    _prog_ready(p, tid, now - 3 * 3600)
    key = f"no_progress:task:#{tid}"

    def busy(on):
        p.db.x("DELETE FROM runs")
        if on:
            p.db.x("INSERT INTO runs(role,started,status) VALUES('worker',?,'running')", (now - 60,))

    wakes = 0
    for i in range(6):                     # the only slot fills and frees every other check
        busy(i % 2 == 1)
        wakes += sum(f"task:#{tid}" in e["keys"] for e in progress.check(p.db, cfg, p.state, now + 60 * i))
        assert key in progress.load(p.db)["raised"]
    assert wakes == 1
    # Healthy for 3 checks but only a few minutes: still raised, shown as not cleared yet.
    busy(True)
    for dt in (360, 420):
        progress.check(p.db, cfg, p.state, now + dt)
    assert progress.load(p.db)["raised"][key]["ok_n"] == 3
    assert "not cleared yet" in "\n".join(progress.digest_lines(p.db, now + 420)[0])
    # Healthy for 3 checks spanning 30 min: cleared, with the time it went healthy.
    progress.check(p.db, cfg, p.state, now + 300 + 1800)
    st = progress.load(p.db)
    assert key not in st["raised"] and st["history"][-1]["key"] == key and st["history"][-1]["cleared"] == now + 300
    # Stuck again after it cleared: a new raise, one wake.
    busy(False)
    got = progress.check(p.db, cfg, p.state, now + 2200)
    assert got and f"task:#{tid}" in got[0]["keys"]
    # clear_n and clear_min are settings: 1 check and 0 min clears at once.
    busy(True)
    progress.check(p.db, {**cfg, "coordinator": {"progress_check": {"clear_n": 1, "clear_min": 0}}}, p.state, now + 2260)
    assert key not in progress.load(p.db)["raised"]


def test_progress_a_task_released_by_its_dependency_starts_its_clock_when_ready(env):
    """Review #3562: a task that waited 2 days behind a dependency was reported stuck for 48 h on the
    first check after the dependency finished, before dispatch could start it."""
    from ttp import progress
    p = make(env)
    now = time.time()
    a = p.db.add_task("Dep", "spec")
    b = p.db.add_task("Waits", "spec", depends_on=[a])
    p.db.x("UPDATE tasks SET created=? WHERE id IN (?,?)", (now - 2 * 86400, a, b))
    progress.check(p.db, p.config(), p.state, now)
    assert str(b) not in progress.load(p.db).get("ready", {})                  # not ready yet: no clock
    p.db.update_task(a, status="done")
    got = progress.check(p.db, p.config(), p.state, now + 60)
    assert not any(f"task:#{b}" in e["keys"] for e in got), got
    assert progress.load(p.db).get("ready", {})[str(b)]["since"] == now + 60
    keys = [k for t in range(2, 8) for e in progress.check(p.db, p.config(), p.state, now + 60 + t * 600)
            for k in e["keys"] if k == f"task:#{b}"]
    assert keys == []                                                  # under task_h since it became ready
    keys = [k for dt in (2 * 3600, 2 * 3600 + 60) for e in progress.check(p.db, p.config(), p.state, now + 60 + dt)
            for k in e["keys"] if k == f"task:#{b}"]
    assert keys == [f"task:#{b}"]                                      # once ready_since + task_h passed: one key


def test_progress_a_task_released_by_start_after_or_a_lifted_pause_starts_its_clock_then(env):
    from ttp import progress
    p = make(env)
    cfg = {"coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    now = time.time()
    t1 = p.db.add_task("Later", "spec", labels=[f"start_after:{now + 3600}"])
    p.db.x("UPDATE tasks SET created=?, not_before=? WHERE id=?", (now - 86400, now + 3600, t1))
    t2 = p.db.add_task("On box-a", "spec", labels=["resource:box-a"])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 86400, t2))
    p.db.set_kv("paused_resources", {"box-a": {"reason": "maintenance", "since": now - 86400, "by": "test"}})
    assert progress.check(p.db, cfg, p.state, now) == []
    assert progress.load(p.db).get("ready", {}) == {}
    # The start_after time passes and the pause is lifted: both ready now, neither stuck.
    p.db.x("UPDATE tasks SET not_before=? WHERE id=?", (now - 1, t1))
    p.db.set_kv("paused_resources", {})
    assert progress.check(p.db, cfg, p.state, now + 60) == []
    assert {k: v["since"] for k, v in progress.load(p.db).get("ready", {}).items()} == {str(t1): now + 60, str(t2): now + 60}
    got = progress.check(p.db, cfg, p.state, now + 60 + 2 * 3600)
    assert got and set(got[0]["keys"]) == {f"task:#{t1}", f"task:#{t2}"}
    # The resource key of a ready task starts there too, not at created.
    p.db.update_task(t1, status="cancelled")
    p.db.set_kv("paused_resources", {"box-b": {"reason": "hold", "since": now, "by": "test"}})
    t3 = p.db.add_task("On box-b", "spec", labels=["resource:box-b"])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 86400, t3))
    rcfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}
    progress.check(p.db, rcfg, p.state, now + 3 * 3600)
    p.db.set_kv("paused_resources", {})
    assert "resource:box-b" not in str(progress.check(p.db, rcfg, p.state, now + 3 * 3600 + 60))
    assert "resource:box-b" in str(progress.check(p.db, rcfg, p.state, now + 5 * 3600 + 60))


def test_progress_a_dispatch_flap_keeps_the_ready_clock_until_gone_long_enough(env):
    """A task dispatched and requeued within the hysteresis keeps when it first became ready; one gone
    for clear_n checks spanning clear_min minutes loses it."""
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Flaky", "spec")
    progress.check(p.db, p.config(), p.state, now)
    for i, status in enumerate(("running", "queued", "running", "running", "queued"), 1):
        p.db.update_task(tid, status=status)
        progress.check(p.db, p.config(), p.state, now + 600 * i)
        assert progress.load(p.db)["ready"][str(tid)]["since"] == now
    got = progress.check(p.db, p.config(), p.state, now + 2 * 3600)
    assert got and f"task:#{tid}" in got[0]["keys"]
    p.db.update_task(tid, status="running")
    for dt in (3 * 3600, 3 * 3600 + 60, 3 * 3600 + 1800):
        progress.check(p.db, p.config(), p.state, now + dt)
    assert str(tid) not in progress.load(p.db)["ready"]
    p.db.update_task(tid, status="queued")
    progress.check(p.db, p.config(), p.state, now + 4 * 3600)
    assert progress.load(p.db)["ready"][str(tid)]["since"] == now + 4 * 3600


def test_progress_probe_stall_wakes_once_escalates_and_clears(env):
    from ttp import progress
    p = make(env)
    probe = "ssh box-a test -e /tmp/ready"
    tid = _prog_waiting(p, "Run the sweep", probe)
    now = time.time()
    cur = lambda t: probe if t["id"] == tid else None
    for i in range(10):
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now - 4000 + i * 180)
    progress.note_probe(p.db, tid, probe, 1, "dup verdict", now - 4000 + 9 * 180)   # the same verdict: not counted
    got = progress.check(p.db, {}, p.state, now, current_probe=cur)
    assert [(e["kind"], e["keys"]) for e in got] == [("stall", [f"probe:#{tid}"])]
    ev = _prog_events(p, "stall")[0]
    assert "not yet 10 times" in ev["text"] and "no such file /tmp/ready" in ev["text"] and ev["task"] == tid
    assert json.loads(ev["data"])[0]["n"] == 10
    for i in range(5):                               # still failing, below double: no new wake
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now + 60 + i)
    assert progress.check(p.db, {}, p.state, now + 120, current_probe=cur) == []
    for i in range(5):                               # 20: twice the count at the last wake
        progress.note_probe(p.db, tid, probe, 1, "no such file /tmp/ready", now + 200 + i)
    got = progress.check(p.db, {}, p.state, now + 300, current_probe=cur)
    assert got and "(escalated)" in got[0]["text"]
    assert progress.check(p.db, {}, p.state, now + 13 * 3600, current_probe=cur)[0]["text"].endswith("(still)")
    progress.forget_probe(p.db, tid)                 # the probe passed
    _prog_clear(p, {}, now + 14 * 3600, cur)
    assert progress.digest_lines(p.db, now + 15 * 3600) == ([], "")
    hist = progress.load(p.db)["history"]
    assert hist[-1]["key"] == f"stall:probe:#{tid}" and hist[-1]["wakes"] == 3
    # A wait on the task's own job is expected to be long: never counted as a stall.
    own = _prog_waiting(p, "Wait for checks", "ttp checks --result abc")
    for i in range(30):
        progress.note_probe(p.db, own, "ttp checks --result abc", 1, "running", now - 7200 + i * 180)
    assert progress.check(p.db, {}, p.state, now + 15 * 3600, current_probe=lambda t: "ttp checks --result abc") == []


def test_progress_flags_a_wait_on_what_it_fixes(env):
    from ttp import progress
    p = make(env)
    p.db.set_kv("paused_resources", {"box-a": {"reason": "broker down", "since": time.time(), "by": "test"}})
    probe = "ttp lock --probe box-a"
    tid = _prog_waiting(p, "Fix the box-a broker", probe, labels=["resource:box-a"])
    now = time.time()
    for i in range(12):
        progress.note_probe(p.db, tid, probe, 75, "box-a is paused", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda t: probe)
    assert got and "waits on what it fixes: box-a" in got[0]["text"] and "box-a is paused" in got[0]["text"]
    assert "waits on what it fixes: box-a" in "\n".join(progress.digest_lines(p.db, now)[0])
    # Its holder waiting on the task counts the same, whatever the task's title says.
    p.db.update_task(tid, status="cancelled")
    p.db.set_kv(progress.STATE_KEY, {})
    p.db.set_kv("paused_resources", {"box-b": {"reason": "hold", "since": now, "by": "test"},
                                     "box-c": {"reason": "hold", "since": now, "by": "test"}})
    t = _prog_waiting(p, "Look into logs", "ttp lock --probe box-b")
    holder = p.db.add_task("Benchmark", "spec", labels=["resource:box-b"], depends_on=[t])
    for i in range(12):
        progress.note_probe(p.db, t, "ttp lock --probe box-b", 75, "busy", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda x: "ttp lock --probe box-b")
    assert got and f"its holder #{holder} waits on this task" in got[0]["text"]
    # A plain wait on a paused resource it does not fix is a stall, not flagged.
    for x in (t, holder):
        p.db.update_task(x, status="cancelled")
    p.db.set_kv(progress.STATE_KEY, {})
    t3 = _prog_waiting(p, "Run the model", "ttp lock --probe box-c")
    for i in range(12):
        progress.note_probe(p.db, t3, "ttp lock --probe box-c", 75, "busy", now - 5000 + i * 300)
    got = progress.check(p.db, {}, p.state, now, current_probe=lambda x: "ttp lock --probe box-c")
    assert got and "fixes" not in got[0]["text"]


def test_progress_one_reader_and_one_streak_detector_for_driver_reports(env):
    """Review #3485 finding 2: the check had its own report file, writer, offset reader and refusal
    streak detector beside `ttp progress report`'s. One refusal streak woke the coordinator twice and
    the offset reader lost lines rotated into progress.1.jsonl."""
    from ttp import progress
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    assert progress.check(p.db, {}, p.state, now) == []                     # no report file: fine
    # #3467's API is the only one: append(state, rec), KINDS the report kinds, settings() its own.
    assert progress.KINDS == ("result", "refusal", "stall", "failure")
    assert progress.settings({})["stall_after"] == 5 and "stall_after" not in progress.check_settings({})
    assert not hasattr(progress, "read_reports") and not hasattr(progress, "report_stalls")
    # A refusal streak: watch_progress's observation is its one wake; the check adds no stall for it.
    _driver_reports(p, os.getpid(), ["refusal"] * 8, key="lock:box-a", t0=now - 600)
    with open(p.state / progress.LOG, "a") as f:
        f.write("not json\n[1,2]\n" + json.dumps({"kind": "bogus", "key": "x"}) + "\n" + '{"kind": "refusal", "key": "lock:bo\n')
    for dt in (0, 60, 120):
        assert progress.check(p.db, {}, p.state, now + dt) == []
    d = Daemon(p.base)
    d.watch_progress(every_s=0)
    d.watch_progress(every_s=0)
    assert [e["kind"] for e in _prog_events(p)] == ["observation"]
    assert progress.load(p.db)["raised"] == {}
    # Its last refusal still names why a resource that work waits on is stuck.
    tid = p.db.add_task("Benchmark on box-a", "spec", labels=["resource:box-a"])
    _prog_ready(p, tid, now - 5 * 3600)
    got = progress.check(p.db, {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}, p.state, now + 180)
    assert got and got[0]["keys"] == ["resource:box-a"] and "refusal: lock busy (exit 75) #7" in got[0]["text"]
    # A result made inside a task's run resets that task's clock, also once rotated into progress.1.jsonl.
    t2 = p.db.add_task("Queued work", "spec")
    _prog_ready(p, t2, now - 5 * 3600)
    rec = progress.record("result", "filler", "checkpoint saved", env={"TTP_TASK": str(t2)}, now=now + 200)
    progress.append(p.state, rec)
    progress.append(p.state, {**rec, "kind": "refusal", "ts": now + 201}, max_bytes=1)   # rotates
    assert rec["task"] == str(t2) and "checkpoint saved" in (p.state / progress.OLD).read_text()
    cfg = {"coordinator": {"progress_check": {"resource_h": 0, "project_h": 0}}}
    assert f"task:#{t2}" not in str(progress.check(p.db, cfg, p.state, now + 300))
    assert float(progress.load(p.db)["results"][f"task:{t2}"]) == rec["ts"]


def test_progress_repeated_run_failures_and_schedule_failures_stall(env):
    from ttp import progress
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Flaky build", "spec")
    for i in range(3):
        p.db.x("INSERT INTO runs(role,task,started,ended,status,exit_code,note) VALUES('worker',?,?,?,'failed',2,?)",
               (tid, now - 900 + i * 200, now - 800 + i * 200, json.dumps({"error": f"link error in obj {i}"})))
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "project_h": 0}}}
    got = progress.check(p.db, cfg, p.state, now)
    assert got and got[0]["keys"] == [f"runs:#{tid}"] and "3 runs in a row" in got[0]["text"]
    assert "link error in obj" in got[0]["text"]
    p.db.x("INSERT INTO runs(role,task,started,ended,status) VALUES('worker',?,?,?,'ok')", (tid, now, now + 10))
    _prog_clear(p, cfg, now + 60)
    assert not any(k.startswith("stall:runs:") for k in progress.load(p.db)["raised"])
    # A schedule failing the same way repeat_n times.
    p.db.x("INSERT INTO schedules(name,kind,every_s) VALUES('nightly','command',3600)")
    for i in range(4):
        progress.note_schedule(p.db, "nightly", f"error: exit {i + 1}", now + i)
    progress.note_schedule(p.db, "nightly", "skipped: budget low", now + 4)   # an expected skip: neutral
    assert not any(e["kind"] == "stall" for e in progress.check(p.db, cfg, p.state, now + 2005))
    progress.note_schedule(p.db, "nightly", "error: exit 9", now + 6)
    got = progress.check(p.db, cfg, p.state, now + 2007)
    assert got and got[0]["keys"] == ["schedule:nightly"] and "error: exit 9" in got[0]["text"]
    progress.note_schedule(p.db, "nightly", "ok", now + 8)                    # a real run ends it
    _prog_clear(p, cfg, now + 2100)
    assert "stall:schedule:nightly" not in progress.load(p.db)["raised"]


def test_progress_leaves_own_daily_cap_skips_to_the_schedule_budget_observation(env):
    """Review #3485 finding 3: skips on a schedule's own daily cap were re-detected from status strings
    and reported a second time beside the daemon's schedule-budget observation; and the daemon's
    schedule-ran step must still record the skip history and count failures."""
    from ttp import progress
    from ttp import schedule as sched
    from ttp.daemon import SCHEDULE_BUDGET_SOURCE, Daemon
    p = make(env)
    d = Daemon(p.base)
    now = time.time()
    p.db.x("INSERT INTO schedules(name,kind,every_s,budget_usd_day) VALUES('summary2','llm',3600,1)")
    row = p.db.one("SELECT * FROM schedules WHERE name='summary2'")
    for _ in range(7):
        d._schedule_ran(row, "skipped: daily budget used", None)
    cfg = {"coordinator": {"progress_check": {"task_h": 0, "resource_h": 0, "project_h": 0}}}
    assert progress.check(p.db, cfg, p.state, now + 1) == []
    assert "summary2" not in progress.load(p.db)["scheds"]
    obs = p.db.q("SELECT * FROM events WHERE source=?", (SCHEDULE_BUDGET_SOURCE,))
    assert len(obs) == 1 and len(sched.skips(p.db, "summary2")) == 7
    # Failures through the same step are counted.
    for i in range(5):
        d._schedule_ran(row, "error: exit 2", None)
    got = progress.check(p.db, cfg, p.state, time.time() + 2)
    assert got and got[0]["keys"] == ["schedule:summary2"]


def test_progress_mute_keeps_a_key_quiet_and_events_raise_a_stuck_turn(env):
    from ttp import coordinator, progress, screen
    p = make(env)
    now = time.time()
    tid = p.db.add_task("Held work", "spec")
    _prog_ready(p, tid, now - 3 * 3600)
    screen.mute(p.db, progress.SOURCE, "no progress since", 24, why="known")
    assert progress.check(p.db, {}, p.state, now) == []
    assert progress.digest_lines(p.db, now)[0]          # still listed in the digest
    assert coordinator.EFFORT_EVENT_TRIGGERS["no_progress"] == coordinator.EFFORT_EVENT_TRIGGERS["stall"] == "stuck"
    assert "progress_check" in __import__("ttp.project", fromlist=["x"]).EXTRA_KEYS["coordinator"]


def test_progress_daemon_tick_runs_the_check(env):
    from ttp import progress
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("Never started", "spec")
    _prog_ready(p, tid, time.time() - 3 * 3600)
    d = Daemon(p.base)
    d.check_progress()
    assert [e["kind"] for e in _prog_events(p)] == ["no_progress"]
    d.check_progress()                                  # throttled, and unchanged anyway
    assert len(_prog_events(p)) == 1
    assert f"no_progress:task:#{tid}" in progress.load(p.db)["raised"]
