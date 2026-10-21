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


def test_a_schedule_name_outside_the_safe_set_is_refused_everywhere_but_a_stored_one_keeps_working(env):
    """Schedule names show in the web app: letters, digits and . _ - only (1-64). upsert, the schedules
    file and schedule_set refuse others; a row stored before the check can still be changed."""
    from ttp import coordinator as coord
    from ttp import schedule as sched
    p = make(env)
    for bad in ("x');alert(1);//", 'a"b', "has space", "", "x" * 65, None):
        with pytest.raises(ValueError, match="not allowed"):
            sched.upsert(p.db, bad, "llm", "1d")
    sched.upsert(p.db, "nightly-build.v2_a", "llm", "1d")
    out = coord.apply(p, [{"type": "schedule_set", "name": "it's", "kind": "llm", "every": "1d", "spec": "go"}])
    assert len(out) == 1 and "schedule_set rejected" in out[0] and "not allowed" in out[0], out
    assert not p.db.one("SELECT name FROM schedules WHERE name=?", ("it's",))
    with pytest.raises(ValueError, match="entry 1: schedule name"):
        sched.parse_file(json.dumps([{"name": "a'b", "kind": "llm", "every": "1d"}]))
    # A legacy row: changed and switched off as before, in upsert and in the file.
    p.db.x("INSERT INTO schedules(name,kind,every_s,enabled,payload) VALUES('old name','llm',86400,1,'{}')")
    sched.upsert(p.db, "old name", "llm", "2d", enabled=False)
    assert p.db.one("SELECT every_s, enabled FROM schedules WHERE name='old name'") == {"every_s": 172800, "enabled": 0}
    assert sched.parse_file(json.dumps([{"name": "old name", "kind": "llm", "every": "1d"}]), {"old name"})


def _run_precheck_schedule(d):
    """run_schedules, then record a prechecked run still going after the tick's short wait."""
    d.run_schedules()
    for run in list(d._sched_runs.values()):
        run.done.wait(30)
    d.run_schedules()


def _prechecked(p, precheck, **extra):
    """An llm schedule `summary`, due now, whose precheck is `precheck`."""
    from ttp import schedule as sched
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up", "precheck": precheck, **extra})
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))


def _summary(p):
    s = p.db.one("SELECT last_status FROM schedules WHERE name='summary'")["last_status"]
    n = p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule' AND labels=?", (json.dumps(["summary"]),))["n"]
    log = (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    return s, n, log


def test_an_llm_precheck_that_exits_0_skips_the_trigger_with_its_first_line(env):
    """The case behind it: a recurring llm schedule spent model runs on empty windows a script could judge."""
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, "echo; echo '  nothing new in the window  '; echo second line")
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "skipped: precheck: nothing new in the window" and n == 0, (status, log)
    row = p.db.one("SELECT last_run, next_run FROM schedules WHERE name='summary'")
    assert row["last_run"] and row["next_run"] > time.time() + 86400 - 120   # a full period on, not owed
    _prechecked(p, "true")   # no output: still a skip, with a reason
    _run_precheck_schedule(d)
    assert _summary(p)[:2] == ("skipped: precheck: exit 0", 0)


@pytest.mark.parametrize("precheck,extra,logged", [
    ("echo busy; echo why >&2; exit 3", {}, "precheck exited rc=3 (why); the run goes ahead"),
    ("sleep 5", {"precheck_timeout_s": 1}, "precheck timed out after 1 s; the run goes ahead"),
])
def test_an_llm_precheck_that_fails_or_times_out_lets_the_run_go_ahead(env, precheck, extra, logged):
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, precheck, **extra)
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "queued" and n == 1, status
    assert f"schedule summary: {logged}" in log, log


def test_an_llm_precheck_that_raises_lets_the_run_go_ahead(env, monkeypatch):
    from ttp import schedrun
    from ttp.daemon import Daemon
    p = make(env)
    _prechecked(p, "true")

    def boom(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(schedrun, "run_group", boom)
    d = Daemon(p.base)
    d.gates = {}
    _run_precheck_schedule(d)
    status, n, log = _summary(p)
    assert status == "queued" and n == 1, status
    assert "schedule summary: precheck failed (OSError: no shell); the run goes ahead" in log, log


def test_an_llm_precheck_is_not_run_when_an_earlier_gate_already_skipped(env, tmp_path):
    from types import SimpleNamespace
    from ttp.daemon import Daemon
    p = make(env)
    ran = tmp_path / "ran"
    d = Daemon(p.base)
    # The open-run gate.
    _prechecked(p, f"touch {ran}")
    tid = p.db.add_task("[summary] open", "x", origin="schedule")
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["summary"]), tid))
    d.gates = {}
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: previous run still open" and not ran.exists()
    p.db.update_task(tid, status="done")
    # The budget gate, then the schedule's daily budget.
    _prechecked(p, f"touch {ran}")
    d.gates = {d.cfg.get("core_provider", "claude"): SimpleNamespace(allow_optional=False, level="tight")}
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: budget tight" and not ran.exists()
    d.gates = {}
    p.db.x("UPDATE schedules SET budget_usd_day=1, next_run=? WHERE name='summary'", (time.time() - 1,))
    p.db.x("INSERT INTO ledger(ts,source,usd) VALUES(?,?,?)", (time.time() - 60, "schedule:summary", 2.0))
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: daily budget used" and not ran.exists()
    # The idle gate: nothing happened since its last run.
    p.db.x("UPDATE schedules SET budget_usd_day=NULL WHERE name='summary'")
    _prechecked(p, f"touch {ran}", skip_if_idle=True)
    p.db.x("UPDATE messages SET ts=?", (time.time() - 2 * 86400,))
    p.db.x("UPDATE runs SET started=?, ended=?", (time.time() - 2 * 86400, time.time() - 2 * 86400))
    p.db.x("UPDATE schedules SET last_run=? WHERE name='summary'", (time.time() - 86400,))
    _run_precheck_schedule(d)
    assert _summary(p)[0] == "skipped: nothing happened since the last run" and not ran.exists()


def test_a_precheck_is_set_and_cleared_by_schedule_set_and_shown_and_validated(env, capsys):
    from ttp import coordinator as coord
    from ttp import schedule as sched
    p = make(env)
    pay = lambda: json.loads(p.db.one("SELECT payload FROM schedules WHERE name='summary'")["payload"])
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "kind": "llm", "spec": "sum up",
                            "precheck": " scripts/window-empty ", "precheck_timeout_s": 30}]) == []
    assert pay()["precheck"] == "scripts/window-empty" and pay()["precheck_timeout_s"] == 30
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "tier": "light"}]) == []
    assert pay()["precheck"] == "scripts/window-empty"   # left out: kept
    from ttp import cli
    cli.main(["schedules", p.name])
    assert "precheck: scripts/window-empty" in capsys.readouterr().out
    assert coord.apply(p, [{"type": "schedule_set", "name": "summary", "precheck": None}]) == []
    assert "precheck" not in pay()
    for kind, payload, why in [("command", {"command": "true", "precheck": "true"}, "only an llm schedule"),
                               ("llm", {"precheck": 3}, "is a shell command"),
                               ("llm", {"precheck": "true", "precheck_timeout_s": 0}, "number of seconds")]:
        with pytest.raises(ValueError, match=why):
            sched.parse_file(json.dumps([{"name": "x", "kind": kind, "every": "1d", "payload": payload}]))


def test_turning_a_schedule_off_or_removing_it_ends_its_mutes_and_the_audit_skips_mutes_of_an_off_schedule(env):
    """A schedule turned off still held its observation mutes, and the daily audit listed them every day as
    overrides held long, though a schedule that does not run has nothing to mute."""
    from ttp import audit, schedule as sched, screen
    p = make(env)
    now = time.time()
    for name in ("watch", "probe", "gone", "live"):
        sched.upsert(p.db, name, "command", "30m", payload={"command": "true", "timeout_s": 30})
        screen.mute(p.db, f"watcher:{name}", "disk nearly full", 48, why="known")
    screen.mute(p.db, "watcher-error:watch", "timed out after", 48, why="known")
    screen.mute(p.db, "daemon:other", "something noisy", 48, why="known")
    active = lambda: {m["source"] for m in screen.mutes(p.db)}

    sched.set_enabled(p.db, "watch", False)   # the web app's switch
    assert "watcher:watch" not in active() and "watcher-error:watch" not in active()
    ended = p.db.q("SELECT status, text FROM events WHERE text LIKE 'mute ended:%watcher:watch%'")
    assert ended and all("schedule turned off" in e["text"] and e["status"] == "handled" for e in ended)
    sched.upsert(p.db, "probe", "command", "30m", enabled=False, payload={"command": "true"})   # schedule_set
    assert "watcher:probe" not in active()
    assert active() == {"watcher:gone", "watcher:live", "daemon:other"}, "other mutes are unchanged"

    sched.file_path(p).parent.mkdir(parents=True, exist_ok=True)
    sched.file_path(p).write_text(json.dumps([sched.entry(r) for r in p.db.q(
        "SELECT * FROM schedules WHERE name!='gone'")]))
    assert sched.sync_file(p, force=True) == (True, None)
    assert active() == {"watcher:live", "daemon:other"}
    assert p.db.q("SELECT 1 FROM events WHERE text LIKE 'mute ended:%watcher:gone%schedule removed%'")

    # A mute left on an off schedule (set before this change, or written directly) is no override held.
    mutes = p.db.kv(screen.MUTES_KEY)
    p.db.set_kv(screen.MUTES_KEY, [{**m, "since": now - 30 * 3600} for m in mutes]
                + [{**mutes[0], "source": "watcher:watch", "since": now - 30 * 3600}])
    held = audit.overrides(p, p.db, now, 12)
    assert any(h.startswith("mute of watcher:live") for h in held), "a running schedule's mute still counts"
    assert any(h.startswith("mute of daemon:other") for h in held)
    assert not any("watcher:watch" in h for h in held), "the off schedule's mute is skipped"
    p.db.x("DELETE FROM schedules WHERE name='live'")
    assert not any("watcher:live" in h for h in audit.overrides(p, p.db, now, 12)), "a gone schedule's too"
