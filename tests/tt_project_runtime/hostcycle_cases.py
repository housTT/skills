# Planned host cycle tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new host-cycle hold tests here, not there.
from __future__ import annotations


def _hold_setup(env, monkeypatch):
    """Two projects on this host (testhost, listed in `ttp machines`): `demo` drains for a reboot,
    `other` is a co-tenant."""
    from ttp import machines as mm
    p, q = make(env), _second_project(env)
    mm.add("testhost")
    for k in ("TTP_PROJECT", "TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    return p, q


def _hold_daemon(base, calls):
    """A Daemon whose dispatch and start_pushes only record that they ran."""
    from ttp.daemon import Daemon
    d = Daemon(base)
    d.dispatch = lambda: calls.append("dispatch")
    d.start_pushes = lambda: calls.append("push")
    d.maybe_coordinate = lambda: calls.append("coordinate")
    return d


def test_drain_reboot_holds_new_runs_checks_and_pushes_in_every_project_on_the_host(env, monkeypatch, capsys, tmp_path):
    from ttp import cli, hostcycle
    from ttp import coordinator as coord
    from ttp import machine_ledger as ml
    from ttp.runner import boot_id
    p, q = _hold_setup(env, monkeypatch)
    calls: list[str] = []
    d = _hold_daemon(q.base, calls)
    d.tick()
    assert "dispatch" in calls and "push" in calls, "no cycle announced: work starts as usual"
    now = time.time()
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    out = capsys.readouterr().out
    (c,) = ml.changes(["testhost"])
    assert c["kind"] == ml.POWER_CYCLE and c["by"] == "demo" and c["boot"] == boot_id() \
        and c["reason"] == "firmware update" and c["at"] == pytest.approx(now + 1, abs=5)
    assert f"machine change #{c['id']}" in out and "planned host cycle recorded" in out
    (f,) = (p.state / hostcycle.FOLDER).glob("*/record.json")
    assert json.loads(f.read_text())["intent"] == c["id"]
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    assert "already announced by demo" in capsys.readouterr().out and len(ml.changes()) == 1, "one intent per cycle"
    # The co-tenant's daemon starts no worker run or push; its coordinator still runs.
    calls.clear()
    d.tick()
    assert "coordinate" in calls and "dispatch" not in calls and "push" not in calls, calls
    # Status and digest say so.
    line = hostcycle.hold_line(c, q)
    assert line.startswith("holding new runs: planned host cycle by demo at ") and line.endswith("(firmware update)")
    assert line in cli.status_text(q) and line in coord.digest(q, {}, [], [])
    # `ttp checks` starts nothing new.
    monkeypatch.setenv("TTP_PROJECT", str(q.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "true"])
    assert e.value.code == 75 and "ttp checks: not started, holding new runs" in capsys.readouterr().err


@pytest.mark.parametrize("lift", ["timeout", "closed", "new_boot", "no_reboot"])
def test_the_host_cycle_hold_lifts(env, monkeypatch, capsys, lift):
    """The hold ends once the intent closes, a new boot is seen, its record says no reboot (the daemon
    closes it), or POWER_CYCLE_LEAD_S after its time; that last adds one low feed line."""
    from ttp import cli, hostcycle, runner
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    cli.main(["drain", "demo", "--reboot", "bus reset", "--wait", "0"])
    capsys.readouterr()
    (c,) = ml.changes()
    calls: list[str] = []
    d = _hold_daemon(q.base, calls)
    now = c["at"]
    assert d.host_cycle_held(now)["id"] == c["id"]
    assert hostcycle.hold(q, c["at"] - ml.POWER_CYCLE_LEAD_S - 1) is None, "not due yet"
    if lift == "timeout":
        now = c["at"] + ml.POWER_CYCLE_LEAD_S
    elif lift == "closed":
        ml.close_change(c["id"], "done")
    elif lift == "new_boot":
        monkeypatch.setattr(runner, "boot_ids", lambda: ("a-later-boot", "a-later-boot"))
    else:
        (f,) = (p.state / hostcycle.FOLDER).glob("*/record.json")
        f.write_text(json.dumps({**json.loads(f.read_text()), "rebooted": False}))
    feed = "SELECT text FROM messages WHERE kind='info' AND severity='low' AND text LIKE '%did not come%'"
    assert d.host_cycle_held(now) is None and hostcycle.hold(q, now) is None
    assert d.host_cycle_held(now) is None
    assert len(q.db.q(feed)) == (1 if lift == "timeout" else 0), "one low feed line, only on a timeout lift"
    if lift == "no_reboot":
        assert not ml.changes() and "no reboot" in ml.changes(open_only=False)[0]["closed_why"]
    if lift != "timeout":   # the tick reads the real clock, still inside the timeout's window
        calls.clear()
        d.tick()
        assert "dispatch" in calls and "push" in calls


def test_drain_reboot_waits_for_and_lists_other_projects_runs_with_its_own_exit_code(env, monkeypatch, capsys):
    from ttp import cli
    from ttp.project import Project
    p, q = _hold_setup(env, monkeypatch)
    rid = q.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(7,'worker','claude',?,'running')",
                 (time.time(),))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.3"])   # exit 0: its own runs decide
    out = capsys.readouterr().out
    assert time.time() - t0 >= 0.3, "the wait lasts while another project's run is in flight"
    assert "other projects on this host: 1 run(s) or push(es) in flight" in out and f"other run {rid} (worker, task #7)" in out
    # The other project's run ends during the wait: drain returns early.
    p.db.set_paused(False)
    threading.Timer(0.2, lambda: Project(q.base).db.x("UPDATE runs SET status='done', ended=? WHERE id=?",
                                                      (time.time(), rid))).start()
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "10"])
    assert time.time() - t0 < 8 and "other projects on this host" not in capsys.readouterr().out
    # Its own run still decides the exit code: 1 at the timeout.
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')", (time.time(),))
    with pytest.raises(SystemExit) as e:
        cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.1"])
    assert e.value.code == 1


def test_drain_reboot_on_an_unlisted_host_records_the_cycle_and_says_others_are_not_held(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p = make(env)
    cli.main(["drain", "demo", "--reboot", "firmware update"])
    cap = capsys.readouterr()
    assert "no entry in `ttp machines list`" in cap.err and "planned host cycle recorded" in cap.out
    assert not ml.changes() and hostcycle.hold(p) is None


def test_a_drain_intent_holds_and_steers_from_its_start_and_after_an_early_return(env, monkeypatch, capsys, tmp_path):
    """--wait longer than POWER_CYCLE_LEAD_S: the co-tenant is held and its workers told from the drain's
    start, not only POWER_CYCLE_LEAD_S before the drain's end; a drain that returns early moves the
    cycle to then, so the hold covers it."""
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(p, "firmware update", 1200, now=t0)
    assert wrote and c["at"] == t0 + 1200 and c["hold_from"] == t0
    assert ml.cycle_window(c)[0] == t0
    for t in (t0, t0 + 1, t0 + 599):
        assert hostcycle.hold(q, t)["id"] == c["id"], t
    assert hostcycle.hold(q, t0 - 1) is None, "not before the drain began"
    tid = q.db.add_task("co-tenant work", "s", kind="code", origin="user")
    (tmp_path / "w").mkdir()
    rid = q.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
                 (tid, "worker", t0, "running", str(tmp_path / "w")))
    _hold_daemon(q.base, []).power_cycle_notices(t0 + 1)
    assert f"machine change #{c['id']}" in (tmp_path / "w" / "steer.md").read_text(), "told at the drain's start"
    ml.close_change(c["id"], "test")
    q.db.x("UPDATE runs SET status='done', ended=? WHERE id=?", (time.time(), rid))
    # The CLI: an idle host drains at once, and the cycle is due then.
    t1 = time.time()
    cli.main(["drain", "demo", "--reboot", "firmware update", "--wait", "1200"])
    capsys.readouterr()
    (c,) = ml.changes()
    assert c["at"] == pytest.approx(t1, abs=5), "the cycle is recorded now, not at the end of the wait"
    assert hostcycle.hold(q)["id"] == c["id"] and not hostcycle.lapsed()
    assert hostcycle.hold(q, c["at"] + ml.POWER_CYCLE_LEAD_S) is None


def test_a_drain_does_not_reuse_an_intent_planned_for_later(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    later = ml.add_power_cycle("testhost", "2d", "other", "planned maintenance")
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0"])
    assert "already announced" not in capsys.readouterr().out
    mine = [c for c in ml.changes() if c["id"] != later["id"]]
    assert len(mine) == 1 and mine[0]["by"] == "demo"
    assert hostcycle.hold(q)["id"] == mine[0]["id"], "held now, by the drain's own intent"
    assert next(c for c in ml.changes() if c["id"] == later["id"])["at"] == later["at"], "the later one is untouched"


def test_a_retried_drain_does_not_lapse_before_its_record(env, monkeypatch):
    """The recipe retries a drain that timed out: each retry reuses the due intent and pushes it on, and
    the record brings it to the cycle itself, so the hold never lapses mid-drain."""
    from ttp import hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    t0 = time.time()
    c, wrote, _ = hostcycle.open_intent(p, "bus reset", 1200, now=t0)
    hostcycle.cycle_now(p, c, now=t0 + 1200)          # timed out: recorded, retried at once
    c2, wrote2, line = hostcycle.open_intent(p, "bus reset", 1200, now=t0 + 1201)
    assert not wrote2 and c2["id"] == c["id"] and c2["at"] == t0 + 2401 and "already announced" in line
    for t in (t0 + 1800, t0 + 2400):   # past the first try's lapse
        assert hostcycle.hold(q, t)["id"] == c["id"] and not hostcycle.lapsed(t), t
    hostcycle.cycle_now(p, c2, now=t0 + 2000)          # drained early this time
    (c3,) = ml.changes()
    assert c3["at"] == t0 + 2000 and c3["expires"] == t0 + 2000 + ml.POWER_CYCLE_AFTER_S
    assert [e["by"] for e in c3["edits"]] == ["demo"] * 3
    assert hostcycle.hold(q, t0 + 2599) and hostcycle.lapsed(t0 + 2600) == [ml.changes()[0]]


def test_resume_withdraws_an_intent_its_own_drain_never_recorded(env, monkeypatch, capsys):
    from ttp import cli, hostcycle
    from ttp import machine_ledger as ml
    p, q = _hold_setup(env, monkeypatch)
    theirs = ml.add_power_cycle("testhost", "2d", "other", "their cycle")
    c, wrote, _ = hostcycle.open_intent(p, "firmware update", 1200)   # the drain was stopped: no record
    assert wrote and c["id"] != theirs["id"]
    cli.main(["resume", "demo"])
    assert f"planned host cycle withdrawn (machine change #{c['id']})" in capsys.readouterr().out
    assert [x["id"] for x in ml.changes()] == [theirs["id"]], "only its own unrecorded intent closes"
    # One its drain recorded stays.
    cli.main(["drain", "demo", "--reboot", "firmware update", "--wait", "0"])
    capsys.readouterr()
    cli.main(["resume", "demo"])
    assert "withdrawn" not in capsys.readouterr().out and len(ml.changes()) == 2


def test_drain_reboot_waits_for_other_projects_pushes_but_not_their_coordinators(env, monkeypatch, capsys):
    from ttp import cli
    p, q = _hold_setup(env, monkeypatch)
    rid = q.db.x("INSERT INTO runs(role,provider,started,status) VALUES('coordinator','claude',?,'running')",
                 (time.time(),))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "10"])
    out = capsys.readouterr().out
    assert time.time() - t0 < 8, "another project's coordinator run is not waited for"
    assert f"other run {rid} (coordinator)" in out, "but it is listed"
    tid = q.db.add_task("t", "s")
    q.db.x("UPDATE tasks SET status='pushing' WHERE id=?", (tid,))
    t0 = time.time()
    cli.main(["drain", "demo", "--reboot", "kernel update", "--wait", "0.3"])
    assert time.time() - t0 >= 0.3 and f"other push of task #{tid}" in capsys.readouterr().out
