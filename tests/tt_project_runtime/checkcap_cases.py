# Host-wide checks cap tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new checks-slot tests (checkcap) here, not there.
from __future__ import annotations


def test_checks_waiter_serves_a_pass_recorded_while_it_waited_and_runs_nothing(env, tmp_path, monkeypatch):
    # The same checks passed elsewhere while these waited for a slot: once they take it, the
    # recorded pass serves and the slot goes to the next waiter.
    import threading
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "DONE_S", 3600.0)   # only the look after taking the slot can find it
    keys = []
    real_key = cli._checks_key
    monkeypatch.setattr(cli, "_checks_key", lambda *a: keys.append(a) or real_key(*a))
    held = checkcap.acquire("other")
    ran, done = tmp_path / "ran", []
    t = threading.Thread(target=lambda: done.append(cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])))
    t.start()
    try:
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists() and keys)
        cli._record_pass(*keys[0], str(run))
    finally:
        held.close()
    t.join(30)
    assert not t.is_alive() and not ran.exists(), "a recorded pass of the same checks runs nothing"
    assert "recorded while these waited for a checks slot" in (run / "checks.log").read_text()
    assert checkcap.line() == "", "the slot it took went back"


def test_checks_waiter_for_the_same_running_checks_waits_outside_the_queue_and_keeps_its_place(env, monkeypatch):
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    twin = checkcap.acquire("twin", same="K")
    later = locks.enqueue(checkcap.slot_dir(), checkcap.RESOURCE, "later", since=2000.0, group="B")
    told, got = [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("w", tell=told.append, since=1000.0, project_name="A",
                                                                    same="K", max_wait_s=20)))
    t.start()
    try:
        assert _wait_for(lambda: any("the same checks run now (twin)" in m for m in told))
        assert checkcap.state()["queued"] == ["later"], "it waits for their result outside the queue"
        twin.close()   # they ended without a pass
        t.join(30)
        assert got and got[0].f is not None, "it queued again at its own arrival time, ahead of a later waiter"
        assert locks.queued(checkcap.slot_dir(), checkcap.RESOURCE) == [later]
        got[0].close()
    finally:
        twin.close()
        locks.dequeue(later)


def test_checks_waiter_for_the_same_running_checks_ends_with_no_slot_once_their_pass_is_recorded(env, monkeypatch):
    import threading
    from ttp import checkcap
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    twin = checkcap.acquire("twin", same="K")
    told, got, passed = [], [], []
    t = threading.Thread(target=lambda: got.append(checkcap.acquire("w", tell=told.append, same="K", max_wait_s=20,
                                                                    done=lambda: bool(passed))))
    t.start()
    try:
        assert _wait_for(lambda: told)
        passed.append(1)
        t.join(30)
        assert got and got[0].f is None and got[0].served and got[0].env() == {}
        assert checkcap.state()["running"] == ["twin"] and checkcap.state()["queued"] == []
    finally:
        twin.close()


def test_checks_slot_is_released_only_after_the_pass_is_recorded(env, tmp_path, monkeypatch):
    # A waiter for the same checks that takes the freed slot must find the pass at once.
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    real, running = cli._record_pass, []
    monkeypatch.setattr(cli, "_record_pass", lambda *a, **k: running.append(checkcap.state()["running"]) or real(*a, **k))
    cli.main(["checks", "--", "true"])
    assert running and running[-1], "the slot is still held while the pass is recorded"
    assert checkcap.line() == ""


def test_checks_never_run_without_a_slot_when_the_pass_that_ended_their_wait_cannot_be_read(env, tmp_path, monkeypatch):
    # done() saw a recorded pass, so acquire returned no slot; the pass was gone by the look after it.
    # The checks queued again instead of running uncapped.
    from ttp import checkcap, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setattr(checkcap, "DONE_S", 0.0)
    held = checkcap.acquire("other")
    served = []

    def recorded(*a, **k):
        if sys._getframe(1).f_code.co_name == "<lambda>":   # done(), asked while waiting
            if not served:
                served.append(1)
                return {"run": "9", "ts": time.time()}
            return None
        if served:
            held.close()   # the pass reads as gone; the slot frees for the next try
        return None

    monkeypatch.setattr(cli, "_recorded_pass", recorded)
    seen = tmp_path / "seen"
    cli.main(["checks", "--", "sh", "-c", f'echo "${checkcap.HELD_ENV}" > {seen}'])
    assert served and seen.read_text().strip() == "1", "the checks ran only once they held a slot"
    assert checkcap.line() == ""


def test_checks_place_carries_only_a_still_waiting_launchs_arrival(tmp_path):
    from ttp import checkcap, cli
    now = time.time()
    old = {"pid": 4242, "arrived": now - 300}
    near = lambda t: abs(t - time.time()) < 5
    assert near(cli._checks_place(tmp_path, old, {})), "no slot wait: they took a slot already"
    (tmp_path / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": 999}))
    assert near(cli._checks_place(tmp_path, old, {})), "the wait is another process's"
    (tmp_path / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": 4242}))
    assert cli._checks_place(tmp_path, old, {}) == now - 300
    assert cli._checks_place(tmp_path, old, {checkcap.ARRIVED_ENV: repr(now - 900)}) == now - 900, "the earlier one"
    for bad in (now + 3600, "junk", "nan", -5):
        assert near(cli._checks_place(tmp_path, {"pid": 4242, "arrived": bad}, {})), bad


def test_restarted_detached_checks_keep_the_queue_place_of_the_ones_still_waiting(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli, locks
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_TASK", "7")
    holder = _cap_holder("other")
    arrivals = lambda: [locks._arrival(t) for t in locks.queued(checkcap.slot_dir(), checkcap.RESOURCE)]
    try:
        cli.main(["checks", "--detach", "--", "true"])
        assert _wait_for(lambda: (run / cli.CHECKS_SLOT_WAIT).exists())
        info = json.loads((run / cli.CHECKS_PID).read_text())
        early = round(time.time() - 600, 3)
        (run / cli.CHECKS_PID).write_text(json.dumps({**info, "arrived": early}))
        cli.main(["checks", "--detach", "--", "true"])
        new = json.loads((run / cli.CHECKS_PID).read_text())
        assert new["pid"] != info["pid"] and new["arrived"] == early, "the restart keeps the waiting one's place"
        assert _wait_for(lambda: len(arrivals()) == 1 and abs(arrivals()[0] - early) < 1e-3), arrivals()
    finally:
        holder.kill()
        holder.wait()
    assert _wait_for(lambda: (run / cli.CHECKS_RC).exists())
    assert (run / cli.CHECKS_RC).read_text().strip() == "0"


def test_daemon_asks_only_an_ended_tasks_still_waiting_checks_to_stop_waiting(env, tmp_path):
    from ttp import cli
    from ttp.daemon import Daemon
    from ttp.runner import proc_start
    p = make(env)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        started = proc_start(proc.pid)

        def run_of(status, wait_pid=None, start=None, updated=None, waits=True):
            tid = p.db.add_task("t", "s", kind="code")
            p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, updated or time.time(), tid))
            run_dir = tmp_path / f"run-{tid}"
            run_dir.mkdir()
            p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
                   (tid, "worker", "fake", time.time(), "ok", str(run_dir)))
            (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": proc.pid, "started": start or started}))
            if waits:
                (run_dir / cli.CHECKS_SLOT_WAIT).write_text(json.dumps({"pid": wait_pid or proc.pid}))
            return tid, run_dir

        stop = {s: run_of(s) for s in ("done", "failed", "cancelled")}
        keep = [run_of("done", waits=False), run_of("done", wait_pid=proc.pid + 100000), run_of("done", start="1"),
                run_of("running"), run_of("waiting"), run_of("done", updated=time.time() - 2 * 86400)]
        d = Daemon(p.base)
        d.reap_waiting_checks(every_s=0)
        for status, (tid, run_dir) in stop.items():
            assert json.loads((run_dir / cli.CHECKS_SLOT_CANCEL).read_text()) == {
                "pid": proc.pid, "started": started, "why": f"task #{tid} ended ({status})"}
        assert not [r for _, r in keep if (r / cli.CHECKS_SLOT_CANCEL).exists()], \
            "checks with a slot, another process and live tasks are left alone"
        path = stop["done"][1] / cli.CHECKS_SLOT_CANCEL
        os.utime(path, (1000, 1000))
        d.reap_waiting_checks(every_s=0)
        assert path.stat().st_mtime == 1000, "the same request is not written again"
    finally:
        proc.kill()
        proc.wait()


def test_a_slot_cancel_request_ends_only_its_own_waiter_and_only_while_it_waits(env, tmp_path, monkeypatch, capsys):
    from ttp import checkcap, cli
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    me = {"pid": os.getpid(), "started": proc_start(os.getpid()), "why": "task #3 ended (done)"}
    assert cli._checks_cancelled(run) == ""
    for other in ({"pid": os.getpid() + 100000}, {"started": "1"}):
        (run / cli.CHECKS_SLOT_CANCEL).write_text(json.dumps({**me, **other}))
        assert cli._checks_cancelled(run) == "", other
    (run / cli.CHECKS_SLOT_CANCEL).write_text(json.dumps(me))
    assert cli._checks_cancelled(run) == "task #3 ended (done)"
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    free = checkcap.acquire("free", stop=lambda: "ended")
    assert free.f is not None, "a slot free at once is taken: only a wait is cancelled"
    with pytest.raises(checkcap.Cancelled, match="ended"):
        checkcap.acquire("w", stop=lambda: "ended", max_wait_s=20)
    assert checkcap.state()["queued"] == []
    ran = tmp_path / "ran"
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--", "sh", "-c", f"touch {ran}"])
    finally:
        free.close()
    assert e.value.code == 1 and not ran.exists()
    assert "not run: stopped waiting for a host-wide checks slot: task #3 ended (done)" in (run / "checks.log").read_text()


def test_checks_extra_slots_follow_the_load_and_never_pass_the_cap_plus_extra(env, monkeypatch):
    import threading
    from ttp import checkcap, locks
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "1")
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    monkeypatch.setattr(checkcap, "cores", lambda: 16)
    load = [3.0]
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    assert checkcap.extra() == 2
    load[0] = 6.0
    assert checkcap.extra() == 1
    load[0] = 9.0
    assert checkcap.extra() == 0
    load[0] = 1.0
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "1")
    assert checkcap.extra() == 1, "never more than checks.load_extra"
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "2")
    held, go = [], threading.Barrier(6)

    def take(i):
        go.wait()
        s = checkcap.acquire(f"t{i}", project_name=f"P{i}", max_wait_s=3)
        if s is not None:
            held.append(s)

    threads = [threading.Thread(target=take, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(held) == 3 and len(locks.held_labels(checkcap.all_paths())) == 3, "racing takers stop at cap plus extra"
    assert "3 running (cap 1 +2 while the load is low)" in checkcap.line()
    load[0] = 9.0
    held.pop().close()
    assert checkcap.acquire("late", max_wait_s=1) is None, "no new slot beyond the cap once the load rose"
    assert len(locks.held_labels(checkcap.all_paths())) == 2, "checks on extra slots run on to their end"
    for s in held:
        s.close()
    s = checkcap.acquire("next", max_wait_s=5)
    assert s is not None and s.f is not None
    s.close()


def test_check_commands_run_niced_where_nice_and_ionice_exist_unless_turned_off(env, monkeypatch):
    from ttp import cli, project
    have = {"nice"}
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name in have else None)
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "19", "pytest"]
    have.add("ionice")
    assert cli._low_priority(["pytest"]) == ["nice", "-n", "19", "ionice", "-c3", "pytest"]
    have.clear()
    assert cli._low_priority(["pytest"]) == ["pytest"], "neither found: run as it is"
    have.update({"nice", "ionice"})
    cli.main(["config", "--account", "checks.nice", "false"])
    assert project.load_account_settings()["checks"]["nice"] is False
    assert cli._low_priority(["pytest"]) == ["pytest"]


def test_checks_default_cap_scales_with_load_headroom_between_floor_and_ceiling(env, monkeypatch):
    from ttp import checkcap, project
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    load = [0.0]
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    assert checkcap.default_range() == (4, 8)
    assert checkcap.limit() == 8, "an idle host gets the ceiling"
    load[0] = 14.0
    assert checkcap.limit() == 8, "headroom for more than the ceiling still stops at it"
    load[0] = 36.0
    assert checkcap.limit() == 7, "(64 - 36) / 4 checks fit"
    load[0] = 43.0
    assert checkcap.limit() == 5, "(64 - 43) / 4 rounds down"
    load[0] = 50.0
    assert checkcap.limit() == 4, "never below the floor of one slot per 16 cores"
    load[0] = 200.0
    assert checkcap.limit() == 4
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
    assert checkcap.limit() == 4, "an unreadable load average gives the floor"
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    monkeypatch.setattr(checkcap, "cores", lambda: 8)
    load[0] = 0.0
    assert checkcap.default_range() == (1, 1) and checkcap.limit() == 1, "a small host runs one at a time"
    monkeypatch.setattr(checkcap, "cores", lambda: 64)
    project.set_account_setting("checks.host_max", 2)
    assert checkcap.limit() == 2, "a set cap wins over an idle host's ceiling"
    load[0] = 200.0
    project.set_account_setting("checks.host_max", 6)
    assert checkcap.limit() == 6, "and over a busy host's floor"
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", "3")
    assert checkcap.limit() == 3, "the environment wins over the setting"


def test_checks_default_cap_shrinking_with_the_load_counts_checks_above_it(env, monkeypatch):
    from ttp import checkcap, locks
    monkeypatch.delenv("TTP_CHECKS_HOST_MAX", raising=False)
    monkeypatch.setenv("TTP_CHECKS_LOAD_EXTRA", "0")
    monkeypatch.setattr(checkcap, "cores", lambda: 32)   # floor 2, ceiling 4
    load = [0.0]
    monkeypatch.setattr(checkcap.os, "getloadavg", lambda: (load[0], 0.0, 0.0))
    held = [checkcap.acquire(f"t{i}", max_wait_s=1) for i in range(4)]
    assert all(s is not None and s.f for s in held)
    assert checkcap.acquire("over", max_wait_s=1) is None, "the ceiling holds"
    load[0] = 30.0   # the floor now: two checks on slots above it still count
    for s in held[:2]:
        s.close()
    assert len(locks.held_labels(checkcap.all_paths())) == 2
    assert checkcap.acquire("late", max_wait_s=1) is None, "two running fill a cap of two"
    held[2].close()
    s = checkcap.acquire("next", max_wait_s=5)
    assert s is not None and s.f is not None
    s.close()
    held[3].close()
