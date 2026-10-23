# `ttp lock` tests (cmd_lock in cli.py, locks.py) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new lock tests here.
from __future__ import annotations


def _lock_env(env, p, **kw):
    return dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base), **kw)


def _lock(run_env, *args, cmd=("true",)):
    return subprocess.Popen([sys.executable, str(TTP), "lock", *args, "--", *cmd], env=run_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _mark(marks, name, flag=None):
    """A command that appends `start <name>` to marks, runs until `flag` exists (when given), then `end <name>`."""
    wait = f"while [ ! -e {shlex.quote(str(flag))} ]; do sleep 0.05; done; " if flag else ""
    return ("sh", "-c", f"echo 'start {name}' >> {shlex.quote(str(marks))}; {wait}"
                        f"echo 'end {name}' >> {shlex.quote(str(marks))}")


def _lock_probe(run_env, *names, extra=()):
    return subprocess.run([sys.executable, str(TTP), "lock", *extra, "--probe", *names], env=run_env,
                          capture_output=True, text=True, timeout=60)


def _queue_len(p, res):
    from ttp import locks, shared
    return len(locks.queued(shared.locks_dir(p, res, p.config()), res))


def _ended(procs, timeout=60):
    out = []
    for x in procs:
        try:
            out.append(x.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            for y in procs:
                y.kill()
            raise AssertionError("a ttp lock never finished (a deadlock?)")
    return out


def test_ttp_lock_takes_two_free_resources_and_shows_both_held_by_the_same_holder(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p, TTP_TASK="7")
    marks, go = tmp_path / "marks", tmp_path / "go"
    m = _lock(run_env, "rig-b,rig-a", cmd=_mark(marks, "both", go))
    try:
        assert _until_true(lambda: marks.exists() and "start both" in marks.read_text()), m.stderr.read()
        held = shared.held(p)
        a = [h.split(": ", 1)[1] for h in held if h.startswith("rig-a: ")]
        b = [h.split(": ", 1)[1] for h in held if h.startswith("rig-b: ")]
        assert len(a) == 1 and a == b and "task #7" in a[0], held
        r = _lock_probe(run_env, "rig-a,rig-b", extra=("--json",))
        rows = json.loads(r.stdout)
        assert r.returncode == 75 and [x["resource"] for x in rows] == ["rig-a", "rig-b"], (r.returncode, r.stdout)
        who = [[(h["holder"], h["pid"], h["command"]) for h in x["holders"]] for x in rows]
        assert all(x["state"] == "busy" for x in rows) and who[0] == who[1] != [], rows
    finally:
        go.touch()
    assert m.wait(timeout=60) == 0
    assert marks.read_text().split("\n")[:2] == ["start both", "end both"]
    r = _lock_probe(run_env, "rig-a,rig-b")
    assert r.returncode == 0 and r.stdout.splitlines() == ["rig-a: free", "rig-b: free"], (r.returncode, r.stdout)
    from ttp import activity
    takes = [e for e in activity.entries(p.state) if e.get("ev") == "take"]
    assert sorted(e["resource"] for e in takes) == ["rig-a", "rig-b"] and len({e["pid"] for e in takes}) == 1


def test_a_first_in_line_multi_waiter_holds_nothing_and_is_not_overtaken_by_later_single_waiters(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    marks, go_h = tmp_path / "marks", tmp_path / "go-h"
    h = _lock(run_env, "rig-a", cmd=_mark(marks, "H", go_h))
    procs = [h]
    try:
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        m = _lock(run_env, "rig-a,rig-b", cmd=_mark(marks, "M"))
        procs.append(m)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 1 and _queue_len(p, "rig-b") == 1)
        # It waits for rig-a only, yet keeps rig-b free: no subset is held while it waits.
        time.sleep(0.3)
        assert not [x for x in shared.held(p) if x.startswith("rig-b: ")], shared.held(p)
        assert _lock_probe(run_env, "rig-b").returncode == 75, "its place in rig-b's queue must show to probes"
        sb = _lock(run_env, "rig-b", cmd=_mark(marks, "SB"))
        sa = _lock(run_env, "rig-a", cmd=_mark(marks, "SA"))
        procs += [sb, sa]
        assert _until_true(lambda: _queue_len(p, "rig-a") == 2 and _queue_len(p, "rig-b") == 2)
        time.sleep(0.5)   # ten polls: rig-b is free, but the earlier multi-waiter is first in its queue
        assert "SB" not in marks.read_text() and sb.poll() is None, marks.read_text()
    finally:
        go_h.touch()
    assert _ended(procs) == [0, 0, 0, 0], [x.stderr.read() for x in procs]
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start")]
    assert starts[:2] == ["H", "M"] and set(starts[2:]) == {"SA", "SB"}, starts


def test_lock_probe_of_two_resources_is_busy_when_either_is_held_or_paused(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    held, go = tmp_path / "held", tmp_path / "go"
    h = _lock(run_env, "rig-b", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    try:
        _wait_for_file(held, h)
        r = _lock_probe(run_env, "rig-a,rig-b")
        assert r.returncode == 75 and r.stdout.splitlines()[0] == "rig-a: free" \
            and r.stdout.splitlines()[1].startswith("rig-b: busy (held by"), r.stdout
    finally:
        go.touch()
    assert h.wait(timeout=30) == 0
    assert _lock_probe(run_env, "rig-a,rig-b").returncode == 0
    p.db.set_kv("paused_resources", {"rig-a": {"reason": "maintenance", "since": time.time()}})
    r = _lock_probe(run_env, "rig-b,rig-a")
    assert r.returncode == 75 and "rig-a: paused (maintenance)" in r.stdout.splitlines(), r.stdout
    # A pause of either refuses the take too, before it queues anywhere.
    t = subprocess.run([sys.executable, str(TTP), "lock", "rig-b,rig-a", "--", "true"], env=run_env,
                       capture_output=True, text=True, timeout=60)
    assert t.returncode == 75 and "rig-a is paused" in t.stderr and _queue_len(p, "rig-b") == 0, t.stderr


def test_two_multi_lockers_in_crossed_order_both_finish_and_never_overlap(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    marks = tmp_path / "marks"
    procs = []
    for i in range(3):
        procs += [_lock(run_env, "rig-a,rig-b", cmd=_mark(marks, f"ab{i}")),
                  _lock(run_env, "rig-b,rig-a", cmd=_mark(marks, f"ba{i}"))]
    assert _ended(procs) == [0] * 6, [x.stderr.read() for x in procs]
    lines = marks.read_text().splitlines()
    assert len(lines) == 12
    assert all(lines[k].split()[0] == "start" and lines[k + 1] == "end " + lines[k].split()[1]
               for k in range(0, 12, 2)), lines


def test_a_multi_lock_honours_its_timeout_and_names_every_resource_in_its_retry_probe(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    held, go = tmp_path / "held", tmp_path / "go"
    h = _lock(run_env, "rig-a", cmd=("sh", "-c", f"touch {shlex.quote(str(held))}; {shlex.join(_until(go))}"))
    try:
        _wait_for_file(held, h)
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b,rig-a", "--", "true"],
                           env=run_env, capture_output=True, text=True, timeout=60)
        assert _queue_len(p, "rig-b") == 0 and _queue_len(p, "rig-a") == 0, "a give-up must leave no ticket"
    finally:
        go.touch()
    assert h.wait(timeout=30) == 0
    assert r.returncode == 75 and "retry_when `ttp lock --probe rig-a,rig-b`" in r.stderr, (r.returncode, r.stderr)


def test_a_multi_lock_keeps_the_device_and_nested_rules(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--shared", "rig-a,board-device", "--", "true"],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "board-device is a device lock" in r.stderr, r.stderr
    assert _lock_probe(run_env, "rig-a,board-device", extra=("--shared",)).returncode == 0
    # Inside a hold of rig-a, a lock of rig-a and rig-b takes rig-b only, and its command sees both held.
    out = tmp_path / "env"
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig-a,rig-b", "--", "sh", "-c",
                        f"echo $TTP_LOCKS_HELD > {shlex.quote(str(out))}"],
                       env={**run_env, "TTP_LOCKS_HELD": "rig-a"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "rig-a is held already by an enclosing ttp lock (exclusive); taking the rest" in r.stderr
    assert out.read_text().split() == ["rig-a,rig-b"]
    r = subprocess.run([sys.executable, str(TTP), "lock", "rig-b,rig-a", "--", "true"],
                       env={**run_env, "TTP_LOCKS_SHARED": "rig-b"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "cannot lock rig-b exclusively inside a shared" in r.stderr, r.stderr


def test_one_resource_lock_runs_its_whole_command_and_probe_parsing_is_unchanged(env, tmp_path):
    from ttp import activity, cli, locks
    assert not hasattr(cli, "_lock_argv"), "argv is parsed as before: several resources are named in one word"
    p = make(env)
    run_env = _lock_env(env, p)
    # A command given without a leading `--` that has an inner one runs whole, under the one lock.
    for args, out in ((["board", "echo", "a", "--", "b"], "a -- b\n"), (["board", "echo", "box", "--", "-r"], "box -- -r\n"),
                      (["--timeout", "5", "board", "--", "echo", "x", "--", "y"], "x -- y\n")):
        r = subprocess.run([sys.executable, str(TTP), "lock", *args], env=run_env, capture_output=True, text=True, timeout=60)
        assert (r.returncode, r.stdout) == (0, out), (args, r.stdout, r.stderr)
        assert sorted({e["resource"] for e in activity.entries(p.state)
                       if e.get("ev") == "take"}) == ["board"], args
    assert locks.lock_probe_parts("ttp lock --probe board") == ([("board", False)], [])
    assert locks.lock_probe_parts("ttp lock --probe --shared a,b && make") == ([("a", True), ("b", True)], ["make"])
    assert locks.probe_resources("ttp lock --probe a,b") == ["a", "b"]
    assert locks.probe_resources("ttp lock --probe a b") == [], "a probe with a stray word is no lock probe, as before"


def _nested_lock(flag, res):
    """A command that waits for `flag`, then runs a `ttp lock <res> -- true` inside it."""
    inner = shlex.join([sys.executable, str(TTP), "lock", res, "--", "true"])
    return ("sh", "-c", f"while [ ! -e {shlex.quote(str(flag))} ]; do sleep 0.05; done; {inner}")


def test_a_lock_holders_nested_lock_never_waits_behind_a_multi_waiter_for_its_own_lock(env, tmp_path):
    # Bug: a first-in-line `ttp lock rig-a,rig-b` waiter kept rig-b's queue, while rig-a's holder ran
    # `ttp lock rig-b` inside its command: each waited for the other, forever.
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    flag = tmp_path / "flag"
    h = _lock(run_env, "--timeout", "0", "rig-a", cmd=_nested_lock(flag, "rig-b"))
    procs = [h]
    try:
        assert _until_true(lambda: any(x.startswith("rig-a: ") for x in shared.held(p)))
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        assert _until_true(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1)
    finally:
        flag.touch()
    assert _ended(procs, timeout=20) == [0, 0], [x.stderr.read() for x in procs]
    # A waiter that holds nothing still never passes it (test above); only lock holders go past it.
    from ttp import locks
    t = locks.enqueue(tmp_path / "q", "r", "w", multi=True, priority=2, group="g")
    try:
        assert locks.is_multi(t) and locks.ticket_priority(t) == 2 and locks.ticket_group(t) == locks.group_id("g")
        u = locks.enqueue(tmp_path / "q", "r", "u", shared=True)
        assert not locks.is_multi(u) and locks.my_turn(tmp_path / "q", "r", u, 1) is False
        assert locks.my_turn(tmp_path / "q", "r", u, 1, past_multi=True)
        locks.dequeue(u)
    finally:
        locks.dequeue(t)


def test_a_whole_run_holders_nested_lock_never_waits_behind_a_multi_waiter_for_its_lock(env, tmp_path):
    from ttp import shared
    p = make(env)
    run_env = _lock_env(env, p)
    go = tmp_path / "go"
    run_hold = _lock(run_env, "rig-a", cmd=_until(go))   # stands for the exclusive task's whole-run hold of rig-a
    procs = [run_hold]
    try:
        assert _until_true(lambda: any(x.startswith("rig-a: ") for x in shared.held(p)))
        w = _lock(run_env, "rig-a,rig-b")
        procs.append(w)
        assert _until_true(lambda: _queue_len(p, "rig-b") == 1 and _queue_len(p, "rig-a") == 1)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "run.json").write_text(json.dumps({"exclusive": [{"resource": "rig-a"}]}))
        inner = _lock({**run_env, "TTP_RUN_DIR": str(run_dir)}, "--timeout", "0", "rig-b")
        procs.append(inner)
        assert inner.wait(timeout=20) == 0, inner.stderr.read()
    finally:
        go.touch()
    assert _ended(procs, timeout=20) == [0, 0, 0], [x.stderr.read() for x in procs]


def test_one_resource_lock_output_is_unchanged(env, tmp_path):
    p = make(env)
    run_env = _lock_env(env, p)
    r = _lock_probe(run_env, "board")
    assert (r.returncode, r.stdout, r.stderr) == (0, "board: free\n", "")
    r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c", "echo hi; echo err >&2"],
                       env=run_env, capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (0, "hi\n", "err\n")
    r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "true"], env={**run_env, "TTP_LOCKS_HELD": "board"},
                       capture_output=True, text=True, timeout=60)
    assert r.stderr == "ttp lock: board is held already by an enclosing ttp lock (exclusive); running the command under it\n"


def _reserve_for(p, res, task, stale=False):
    """The daemon's reservation of res for task (_resources_free(reserve=True)); its path."""
    from ttp import hold, locks, shared
    mark = locks.reserve_path(shared.locks_dir(p, res, p.config()), res)
    locks.reserve(mark, shared.holder(p, res, hold.who(task), p.config()))
    if stale:
        r = json.loads(mark.read_text())
        mark.write_text(json.dumps({**r, "ts": time.time() - locks.RESERVE_STALE_S - 5}))
    return mark


def test_a_tasks_own_driver_takes_the_slot_its_task_reserved_and_other_tasks_still_wait(env, tmp_path):
    """A queued exclusive task's reservation holds off other tasks' commands, but its own detached driver
    (same TTP_TASK) takes the slot ahead of them, as the task's run would, and clears the reservation."""
    from ttp import locks
    p = make(env)
    marks, go_h, go_d = tmp_path / "marks", tmp_path / "go-h", tmp_path / "go-d"
    h = _lock(_lock_env(env, p, TTP_TASK="1"), "rig-a", cmd=_mark(marks, "H", go_h))
    procs = [h]
    try:
        assert _until_true(lambda: marks.exists() and "start H" in marks.read_text())
        mark = _reserve_for(p, "rig-a", 7)
        o = _lock(_lock_env(env, p, TTP_TASK="8"), "rig-a", cmd=_mark(marks, "O"))
        procs.append(o)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 1)
        d = _lock(_lock_env(env, p, TTP_TASK="7"), "rig-a", cmd=_mark(marks, "D", go_d))
        procs.append(d)
        assert _until_true(lambda: _queue_len(p, "rig-a") == 2)
        go_h.touch()
        assert _until_true(lambda: "start D" in marks.read_text()), (marks.read_text(), d.stderr)
        assert locks.reserved_by(mark) is None, "the driver left its task's reservation behind"
        time.sleep(0.3)
        assert "start O" not in marks.read_text() and o.poll() is None, marks.read_text()
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK="9"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        _reserve_for(p, "rig-b", 7)
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-b", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK="9"), capture_output=True, text=True, timeout=60)
        assert r.returncode == 75 and "reserved for task #7" in r.stderr, (r.returncode, r.stderr)
    finally:
        go_h.touch()
        go_d.touch()
    assert _ended(procs) == [0, 0, 0], [x.stderr.read() for x in procs]
    starts = [ln.split()[1] for ln in marks.read_text().splitlines() if ln.startswith("start")]
    assert starts == ["H", "D", "O"], starts


def test_a_stale_reservation_for_the_drivers_own_task_behaves_as_before(env, tmp_path):
    from ttp import locks
    p = make(env)
    mark = _reserve_for(p, "rig-a", 7, stale=True)
    before = mark.read_text()
    for task in ("7", "8"):
        r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "rig-a", "--", "true"],
                           env=_lock_env(env, p, TTP_TASK=task), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (task, r.stderr)
    assert mark.read_text() == before and locks.reserved_by(mark) is None, "a stale reservation was touched"


def test_a_multi_lock_passes_only_its_own_tasks_reservations_all_or_nothing(env, tmp_path):
    from ttp import locks, shared
    p = make(env)
    mark = _reserve_for(p, "rig-a", 7)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="8"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "reserved for task #7" in r.stderr, (r.returncode, r.stderr)
    other = _reserve_for(p, "rig-b", 9)
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="7"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "rig-b (reserved for task #9)" in r.stderr, (r.returncode, r.stderr)
    assert "rig-a (reserved" not in r.stderr and locks.reserved_by(mark) == "task #7", r.stderr
    assert not shared.held(p) or all("reserved for" in x for x in shared.held(p)), "a subset was held"
    other.unlink()
    r = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "5", "rig-a,rig-b", "--", "true"],
                       env=_lock_env(env, p, TTP_TASK="7"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and locks.reserved_by(mark) is None, (r.returncode, r.stderr)
