# Detached job tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp detach` liveness tests here, not there.
from __future__ import annotations


def _ssh_on_path(tmp_path, monkeypatch):
    """An `ssh` on PATH that runs the remote command here with sh, or fails as an unreachable host
    (exit 255) while the returned flag file exists."""
    bindir, down = tmp_path / "sshbin", tmp_path / "ssh-down"
    bindir.mkdir()
    shim = bindir / "ssh"
    shim.write_text(f'#!/bin/sh\n[ -e {shlex.quote(str(down))} ] && exit 255\nfor last; do :; done\nexec sh -c "$last"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return down


def _remote_job(tmp_path, **remote):
    """The .rc path of a local job whose detached.json entry carries this remote contract."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    rc = run_dir / "job.rc"
    (run_dir / "detached.json").write_text(json.dumps([{"name": "job", "rc": str(rc), "remote": remote}]))
    return rc


def _dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_a_remote_hosts_changed_boot_id_is_a_reset_verdict(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    assert detach.remote_start("box") == boot_id()
    rc = _remote_job(tmp_path, host="box", boot="a-boot-before-the-reset", pid=str(os.getpid()), marker="")
    why = detach.remote_verdict(rc, {})
    assert why and why.startswith("remote reset:") and "box" in why
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(os.getpid()), marker="")
    assert detach.remote_verdict(rc, {}) is None, "a live remote pid on the same boot was called dead"


def test_a_remote_pid_gone_without_its_marker_is_a_verdict_only_on_the_second_probe(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    pidfile = tmp_path / "driver.pid"
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(pidfile), marker=str(tmp_path / "DONE"))
    memo: dict = {}
    assert detach.remote_verdict(rc, memo) is None, "a pid file not written yet is no dead driver"
    pidfile.write_text(f"{_dead_pid()}\n")
    assert detach.remote_verdict(rc, memo) is None, "one probe that reads the pid dead ended the job"
    why = detach.remote_verdict(rc, memo)
    assert why and why.startswith("remote driver gone:") and "DONE is missing" in why
    pidfile.write_text(f"{os.getpid()}\n")   # alive again (a restarted driver): the count starts over
    assert detach.remote_verdict(rc, memo) is None and "dead" not in memo


def test_a_remote_completion_marker_is_never_a_dead_verdict(env, tmp_path, monkeypatch):
    from ttp import detach
    from ttp.runner import boot_id
    _ssh_on_path(tmp_path, monkeypatch)
    (tmp_path / "DONE").write_text("0\n")
    rc = _remote_job(tmp_path, host="box", boot=boot_id(), pid=str(_dead_pid()), marker=str(tmp_path / "DONE"))
    memo: dict = {}
    assert [detach.remote_verdict(rc, memo) for _ in range(4)] == [None] * 4


def test_an_unreachable_remote_host_is_no_proof_of_death(env, tmp_path, monkeypatch):
    from ttp import detach
    down = _ssh_on_path(tmp_path, monkeypatch)
    down.touch()
    assert detach.remote_start("box") == ""
    rc = _remote_job(tmp_path, host="box", boot="a-boot-before-the-reset", pid=str(_dead_pid()), marker="")
    memo: dict = {}
    assert [detach.remote_verdict(rc, memo) for _ in range(4)] == [None] * 4
    # Unreachable at start: the first answer names the boot, and a later change of it is a reset.
    down.unlink()
    rc = _remote_job(tmp_path, host="box", boot="", pid="", marker="")
    memo = {}
    assert detach.remote_verdict(rc, memo) is None and memo["boot"]
    memo["boot"] = "a-boot-before-the-reset"
    assert (detach.remote_verdict(rc, memo) or "").startswith("remote reset:")


def test_remote_pid_and_marker_need_remote_host(env, tmp_path):
    r = subprocess.run([sys.executable, str(TTP), "detach", "job", "--remote-pid", "5", "--", "true"],
                       capture_output=True, text=True, env=dict(os.environ, TTP_RUN_DIR=str(tmp_path)))
    assert r.returncode != 0 and "go with --remote-host" in r.stderr


def test_a_keeper_ends_a_job_whose_remote_driver_died_and_lets_its_hold_go(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import detach, hold, locks
    from ttp.daemon import Daemon
    from ttp.runner import boot_id
    down = _ssh_on_path(tmp_path, monkeypatch)
    mine = _board_task(p, "flash")
    run_dir, release, pidfile = p.runs / "51", tmp_path / "release", tmp_path / "driver.pid"
    driver = subprocess.Popen(["sleep", "600"])   # stands in for the driver on the remote host
    pidfile.write_text(f"{driver.pid}\n")
    paths = Daemon(p.base)._slot_paths("board")
    try:
        _exclusive_run_detaching(p, mine["id"], 51, run_dir, _until(release),
                                 opts=("--remote-host", "box", "--remote-pid", str(pidfile),
                                       "--remote-marker", str(tmp_path / "DONE")))
        entry = json.loads((run_dir / "detached.json").read_text())[0]
        assert entry["remote"] == {"host": "box", "boot": boot_id(), "pid": str(pidfile),
                                   "marker": str(tmp_path / "DONE")}
        rc = pathlib.Path(entry["rc"])
        assert hold.read(run_dir)["state"] == "holding" and not locks.any_free(paths)
        # The host stops answering and its driver dies: unreachable proves nothing, the hold stays.
        down.touch()
        driver.kill()
        driver.wait()
        time.sleep(1)
        assert not locks.any_free(paths) and not locks.job_ended(rc), "an unreachable host ended the job"
        down.unlink()
        assert _until_true(lambda: locks.any_free(paths)), "a dead remote driver's hold was kept"
        st, why = detach.state(rc)
        assert st == "lost" and why.startswith("remote driver gone:")
        rec = hold.read(run_dir)
        assert rec["state"] == "released" and "every detached job of the run ended" in rec["why"]
        assert "remote driver gone" in rec["why"]
        assert not rc.exists(), "the local driver was not ended"
    finally:
        release.touch()
        if driver.poll() is None:
            driver.kill()
