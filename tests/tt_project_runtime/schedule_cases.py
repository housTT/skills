# Schedules area: a schedule that starts whole periods late (the host or daemon down, a tick held up) is
# reported with its missed runs and likely cause, never silently resumed. Exec'd into
# test_tt_project_runtime.py: make and its imports are in scope.


def test_a_schedule_counts_whole_periods_missed_but_not_short_delays_sleep_or_a_first_run(env):
    from ttp import schedule as sched
    now = time.time()
    row = lambda late, every=1800, last=now - 86400: {"next_run": now - late, "every_s": every, "last_run": last}
    assert sched.missed(row(60), now) == 0                       # on time
    assert sched.missed(row(3 * 3600), now) == 6                 # 3 h late every 30 min
    assert sched.missed(row(50 * 60, every=300), now) == 0       # under GAP_MIN_S, though 10 periods
    assert sched.missed(row(3 * 3600, last=None), now) == 0      # never ran: nothing it recorded has a hole
    assert sched.missed(row(3 * 3600), now, [[now - 4 * 3600, now - 60]]) == 0   # the host slept through it
    assert sched.missed(row(3 * 3600), now, [[now - 3 * 3600, now - 2 * 3600]]) == 4


def _late_schedule(p, late_s, every="30m"):
    from ttp import schedule as sched
    sched.upsert(p.db, "watch", "command", every, payload={"command": "true", "timeout_s": 30})
    now = time.time()
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='watch'", (now - late_s - 1800, now - late_s))


def _gap_events(p):
    return p.db.q("SELECT severity, status, text FROM events WHERE source='daemon:schedule-gap'")


def test_an_abrupt_reboot_that_stopped_a_schedule_wakes_the_coordinator_once_with_the_cause(env):
    """The case behind this check: the host went down abruptly for ~40 h, the daemon came back at boot and
    the schedule simply resumed, so nothing but its own output's hole showed the missed runs."""
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 40 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    now = time.time()
    d.boot_at, d._started = now - 120, now - 60
    p.db.set_kv("boot_prev", {"boot": d.boot, "prev_boot": "earlier-boot", "held": [], "shutdown": "abrupt",
                              "last_heartbeat": now - 40 * 3600 - 600})
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued" and ev[0]["severity"] in ("normal", "high")
    assert "watch 80 (every 30m" in ev[0]["text"] and "host went down (abrupt" in ev[0]["text"]
    assert p.db.one("SELECT last_run FROM schedules WHERE name='watch'")["last_run"] >= now - 5   # it ran
    d.run_schedules()
    assert len(_gap_events(p)) == 1   # on time again: told once


def test_a_schedule_held_up_while_the_daemon_ran_wakes_the_coordinator(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    d._started = time.time() - 10 * 3600
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued" and "daemon was running" in ev[0]["text"]


def test_a_clean_daemon_stop_is_a_record_that_wakes_nobody(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    d = Daemon(p.base)
    d.gates = {}
    now = time.time()
    d._started, d._last_stop = now - 60, {"boot": d.boot, "ts": now - 3 * 3600 - 300}
    d.run_schedules()
    ev = _gap_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "record" and "daemon was stopped" in ev[0]["text"]


def test_a_schedule_missed_while_the_host_slept_is_no_gap(env):
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    now = time.time()
    p.db.set_kv("host_sleeps", [[now - 4 * 3600, now - 30]])
    d = Daemon(p.base)
    d.gates = {}
    d._started = now - 10 * 3600
    d.run_schedules()
    assert not _gap_events(p)


def test_overlapping_sleep_and_pause_windows_are_taken_off_once(env):
    from ttp import schedule as sched
    now = time.time()
    row = {"next_run": now - 5 * 3600, "every_s": 1800, "last_run": now - 86400}
    both = [[now - 3 * 3600, now - 3600], [now - 2 * 3600, now - 1800]]   # 2.5 h off, not 3 h
    assert sched.missed(row, now, both) == 5


def test_a_project_pause_while_the_daemon_ran_is_no_gap(env):
    """Nothing runs while the project is paused (Daemon.tick returns before run_schedules), by design:
    after the resume every schedule is hours late, which is no held-up tick."""
    from ttp import projpause
    from ttp.daemon import Daemon
    p = make(env)
    _late_schedule(p, 3 * 3600)
    now = time.time()
    projpause.pause(p.db, "user", "maintenance", now=now - 3 * 3600 - 60)   # as db.set_paused does
    p.db.set_kv("paused", True)
    projpause.resume(p.db, "user", now=now - 30)
    p.db.set_kv("paused", False)
    d = Daemon(p.base)
    d.gates = {}
    d._started = now - 10 * 3600
    d.run_schedules()
    assert not _gap_events(p)
    assert p.db.one("SELECT last_run FROM schedules WHERE name='watch'")["last_run"] >= now - 5   # it ran


def test_a_schedule_disabled_then_re_enabled_is_no_gap(env):
    """Re-enabling (the web app, sched.set_enabled; or the schedules file, upsert) starts from the next
    slot from now, so the hours it was off are no missed runs."""
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    p = make(env)
    for how in ("web", "file"):
        _late_schedule(p, 3 * 3600)
        p.db.x("UPDATE schedules SET enabled=0 WHERE name='watch'")
        now = time.time()
        if how == "web":
            sched.set_enabled(p.db, "watch", True)
        else:
            sched.upsert(p.db, "watch", "command", "30m", payload={"command": "true", "timeout_s": 30})
        row = p.db.one("SELECT enabled, next_run FROM schedules WHERE name='watch'")
        assert row["enabled"] == 1 and now < row["next_run"] <= time.time() + 1800, how
        d = Daemon(p.base)
        d.gates = {}
        d._started = now - 10 * 3600
        d.run_schedules()
        assert not _gap_events(p), how
    sched.set_enabled(p.db, "watch", False)   # turning one off keeps its next run
    assert p.db.one("SELECT enabled FROM schedules WHERE name='watch'")["enabled"] == 0


def _gap_issue(p):
    return p.db.one("SELECT * FROM issues WHERE source='daemon:schedule-gap'")


def test_issues_close_ends_a_stale_schedule_gap_with_its_reason_and_a_new_sighting_reopens_it(env, capsys,
                                                                                               monkeypatch):
    """A schedule-gap issue that nothing closes by itself (say after a host move) is closed with
    `ttp issues close`, not a DB write: the reason and time go in its history and the activity feed,
    and the same condition seen again reopens it and wakes, so a close never hides a live one."""
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    for k in ("TTP_RUN_DIR", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    p = make(env)
    d = Daemon(p.base)
    text = "Schedules missed runs since 10:00: watch 6. Likely cause: the daemon was not running."
    d.observe("daemon:schedule-gap", text, "normal", whole=True, key="schedule-gap:1700000000")
    issue = _gap_issue(p)
    assert issue["status"] == "open" and issue["watch_key"] == "schedule-gap:1700000000"
    cli.main(["issues", "list", "--project", "demo"])
    assert f"#{issue['id']} " in capsys.readouterr().out
    cli.main(["issues", "close", "schedule-gap:1700000000", "--why", "host moved; the gap is known",
              "--project", "demo"])
    assert f"closed issue #{issue['id']}" in capsys.readouterr().out
    row = _gap_issue(p)
    assert row["status"] == "fixed" and row["closed"] and "host moved; the gap is known" in row["cleared_why"]
    [h] = json.loads(row["history"])
    assert h["action"] == "closed" and h["why"] == "host moved; the gap is known" and h["by"] and h["ts"]
    feed = [m for m in alerts.feed(p.db, time.time()) if f"issue #{issue['id']}" in m["text"]]
    assert feed and "host moved" in feed[0]["text"]
    cli.main(["issues", "list", "--project", "demo"])
    assert "no open issues" in capsys.readouterr().out
    cli.main(["issues", "close", str(issue["id"]), "--why", "again", "--project", "demo"])
    assert "already closed" in capsys.readouterr().out
    assert len(json.loads(_gap_issue(p)["history"])) == 1
    queued = len(p.db.q("SELECT id FROM events WHERE source='daemon:schedule-gap' AND status='queued'"))
    d.observe("daemon:schedule-gap", text, "normal", whole=True, key="schedule-gap:1700000000")
    assert _gap_issue(p)["status"] == "open" and _gap_issue(p)["closed"] is None
    assert [h["action"] for h in json.loads(_gap_issue(p)["history"])] == ["closed", "reopened"]
    assert len(p.db.q("SELECT id FROM events WHERE source='daemon:schedule-gap' AND status='queued'")) == queued + 1


def test_issues_close_refuses_an_unknown_issue_an_empty_why_and_another_projects_issue_in_a_run(env, capsys,
                                                                                                 monkeypatch):
    from ttp import cli
    from ttp.daemon import Daemon
    for k in ("TTP_RUN_DIR", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    p = make(env)
    Daemon(p.base).observe("daemon:schedule-gap", "Schedules missed runs since 10:00: watch 6.", "normal",
                           whole=True, key="schedule-gap:1")
    iid = _gap_issue(p)["id"]
    for argv in (["issues", "close", "9999", "--why", "x"], ["issues", "close", "no-such-key", "--why", "x"],
                 ["issues", "close", str(iid), "--why", "  "], ["issues", "close", str(iid)]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv + ["--project", "demo"])
        assert e.value.code != 0, argv
    assert _gap_issue(p)["status"] == "open" and _gap_issue(p)["history"] is None
    assert not p.db.q("SELECT id FROM messages WHERE text LIKE 'Closed issue%'")
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"] / "run"))
    with pytest.raises(SystemExit):  # a run with no TTP_PROJECT cannot name a project either
        cli.main(["issues", "close", str(iid), "--why", "no project", "--project", "demo"])
    assert _gap_issue(p)["status"] == "open"
    monkeypatch.chdir(p.base)  # no --project: the resolved project is named, never 'None'
    with pytest.raises(SystemExit):
        cli.main(["issues", "close", str(iid), "--why", "no project"])
    err = capsys.readouterr().err
    assert "None" not in err and ("demo" in err or str(p.base) in err)
    monkeypatch.setenv("TTP_PROJECT", str(env["tmp"] / "another-project"))
    monkeypatch.setenv("TTP_TASK", "7")
    with pytest.raises(SystemExit):
        cli.main(["issues", "close", str(iid), "--why", "not mine", "--project", "demo"])
    assert _gap_issue(p)["status"] == "open"
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    cli.main(["issues", "close", f"#{iid}", "--why", "stale", "--project", "demo"])
    assert json.loads(_gap_issue(p)["history"])[0]["by"] == "task #7"


def test_an_issues_table_from_before_history_gains_the_column(env):
    import sqlite3
    from ttp.db import DB
    path = env["tmp"] / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, source TEXT, "
              "first_seen REAL, last_seen REAL, count INTEGER DEFAULT 1, title TEXT, severity TEXT DEFAULT 'normal', "
              "status TEXT DEFAULT 'open', task INTEGER, screen TEXT)")
    c.commit()
    c.close()
    db = DB(path)
    assert "history" in {r["name"] for r in db.q("PRAGMA table_info(issues)")}
