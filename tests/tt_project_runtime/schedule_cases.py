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
