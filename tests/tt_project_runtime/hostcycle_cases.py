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
    assert f"other projects on this host: 1 run(s) in flight" in out and f"other run {rid} (worker, task #7)" in out
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
