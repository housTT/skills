# Driver progress report tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new `ttp progress report` tests here, not there.
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
