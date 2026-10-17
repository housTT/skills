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


def test_stop_records_its_reason_before_a_reader_can_see_the_job_gone(tmp_path, monkeypatch):
    from ttp import detach
    rc = _remote_job(tmp_path)
    seen = []
    # A reader that looks the moment the kill lands (its lock is free) must find the stop's reason.
    monkeypatch.setattr(detach.os, "killpg", lambda pid, sig: seen.append(detach.state(rc)))
    detach.stop(rc, 12345, "remote driver gone: test")
    assert seen == [("lost", "remote driver gone: test")]
    assert detach.state(rc) == ("lost", "remote driver gone: test")


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


def _remote_mirror_job(env, tmp_path, script, name="drv"):
    """A `ttp detach --remote` job started from run 61 through the ssh stub: (project, run dir, remote
    folder, probe argv, ssh env)."""
    p = _device_project(env, device=False)
    run_dir = p.runs / "61"
    run_dir.mkdir(parents=True)
    e = _ssh_stub(tmp_path)
    rdir = tmp_path / "remote"
    r = _ttp_run(p, "detach", "--remote", "box-m", "--dir", str(rdir), name, "--", "sh", "-c", script,
                 env={**e, "TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0, r.stderr
    return p, run_dir, rdir, _remote_probe(r.stdout), e


def test_detach_remote_check_mirrors_the_rc_and_reads_it_once_the_host_is_unreachable(env, tmp_path):
    """The reported failure: a remote job's .log and .rc stayed on a host that then reset, so its probe
    returned 255 forever though the job had finished. The probe now mirrors both into the run folder,
    and with ssh failing it reads the mirrored end: 4 for a failure, 0 for a pass, never 255."""
    p, run_dir, rdir, probe, e = _remote_mirror_job(env, tmp_path, "printf 'out\\377\\n'; exit 3")
    assert probe[probe.index("--mirror") + 1] == str(run_dir.resolve()), probe
    meta = json.loads((run_dir / "drv.remote.json").read_text())
    assert meta["host"] == "box-m" and meta["path"] == str(rdir / "drv") and "end" not in meta
    assert _wait_for_path(rdir / "drv.rc")
    first = _ttp_run(p, *probe[1:], env=e)   # the daemon's probe env carries no TTP_RUN_DIR
    assert first.returncode == 4 and "drv: job finished rc=3" in first.stdout, first.stdout
    assert (run_dir / "drv.remote.rc").read_text().strip() == "3"
    assert (run_dir / "drv.remote.log").read_bytes() == b"out\xff\n", "the log tail is mirrored byte for byte"
    assert json.loads((run_dir / "drv.remote.json").read_text())["end"] == {"rc": "3"}
    down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
    assert down.returncode == 4, down.stdout
    assert "job finished rc=3, as mirrored before box-m stopped answering" in down.stdout, down.stdout
    # The check alone with the env's TTP_RUN_DIR mirrors there too; a mirror is never another job's.
    (run_dir / "drv.remote.json").write_text(json.dumps({"host": "other", "path": str(rdir / "drv"), "end": {"rc": "0"}}))
    assert _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"}).returncode == 255
    assert _ttp_run(p, "detach", "--check", str(rdir / "drv.rc"), "--mirror", str(run_dir)).returncode == 2
    r = _ttp_run(p, "detach", "--remote", "box-m", "--dir", str(rdir), "ok", "--", "echo", "fine",
                 env={**e, "TTP_RUN_DIR": str(run_dir)})
    assert r.returncode == 0 and _wait_for_path(rdir / "ok.rc"), r.stderr
    ok = _remote_probe(r.stdout)
    assert _ttp_run(p, *ok[1:], env=e).returncode == 0
    assert _ttp_run(p, *ok[1:], env={**e, "SSH_STUB_FAIL": "1"}).returncode == 0


def test_detach_remote_mirror_keeps_a_bounded_log_tail_sent_only_when_it_grew(env, tmp_path, monkeypatch):
    from ttp import cli, detach
    big = detach.MIRROR_TAIL_BYTES * 3
    p, run_dir, rdir, probe, e = _remote_mirror_job(
        env, tmp_path, f"head -c {big} /dev/zero | tr '\\0' a; echo END; exit 0")
    assert _wait_for_path(rdir / "drv.rc")
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    sent = []
    real = cli._ssh_sh
    monkeypatch.setattr(cli, "_ssh_sh", lambda *a, **k: sent.append(real(*a, **k)) or sent[-1])
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._detach_check_remote("box-m", [str(rdir / "drv")], mirror=str(run_dir)) == 0
    tail = (run_dir / "drv.remote.log").read_bytes()
    assert len(tail) == detach.MIRROR_TAIL_BYTES and tail == (rdir / "drv.log").read_bytes()[-len(tail):]
    assert tail.endswith(b"END\n")
    assert len(sent[-1].stdout) < detach.MIRROR_TAIL_BYTES * 2, "more than the tail crossed ssh"
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli._detach_check_remote("box-m", [str(rdir / "drv")], mirror=str(run_dir)) == 0
    assert "tail same" in sent[-1].stdout and len(sent[-1].stdout) < 200, "an unchanged log was sent again"
    assert (run_dir / "drv.remote.log").read_bytes() == tail


def test_detach_remote_mirror_unreachable_before_the_end_stays_255_and_the_keeper_refreshes(env, tmp_path, monkeypatch):
    """A host unreachable before any end was mirrored proves nothing: 255 (asleep), naming the log tail
    kept so far. The hold keeper's probe round refreshes the run's mirrors, so the end is caught while
    the host still answers even with no check running."""
    from ttp import detach, hold
    go = tmp_path / "go"
    p, run_dir, rdir, probe, e = _remote_mirror_job(
        env, tmp_path, f"echo step1; while [ ! -e {go} ]; do sleep 0.1; done; echo step2; exit 5")
    pid = int((rdir / "drv.pid").read_text())
    try:
        assert _wait_for_path(rdir / "drv.log") and _until_true(lambda: (rdir / "drv.log").read_text().strip())
        assert _ttp_run(p, *probe[1:], env=e).returncode == 1
        assert (run_dir / "drv.remote.log").read_text() == "step1\n"
        down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
        assert down.returncode == 255 and "ssh to box-m failed" in down.stdout, down.stdout
        assert "last log tail mirrored here" in down.stdout and not (run_dir / "drv.remote.rc").exists()
        go.touch()
        assert _wait_for_path(rdir / "drv.rc")
    finally:
        _kill_driver(pid)
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    hold._remote_round([], {}, [], run_dir)
    assert (run_dir / "drv.remote.rc").read_text().strip() == "5"
    assert (run_dir / "drv.remote.log").read_text() == "step1\nstep2\n"
    monkeypatch.setenv("SSH_STUB_FAIL", "1")
    detach.mirror_refresh(run_dir)   # an ended mirror is not asked again; an unreachable host changes nothing
    down = _ttp_run(p, *probe[1:], env={**e, "SSH_STUB_FAIL": "1"})
    assert down.returncode == 4 and "job finished rc=5, as mirrored" in down.stdout, down.stdout


def test_detach_mirror_probe_reads_the_verdict_before_the_tail(env, tmp_path, monkeypatch):
    """A job that writes its last lines after the verdict was read must still have them in the mirrored
    tail, and the verdict stays the answer's last line."""
    from ttp import cli, detach
    e = _ssh_stub(tmp_path)
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    job = tmp_path / "job"
    (tmp_path / "job.log").write_text("early\n")
    # The fake check reports the job running, then the job writes its last line (as if between steps).
    monkeypatch.setattr(cli, "_REMOTE_CHECK", f'echo "running 1"\necho late >>{tmp_path}/job.log\n')
    d = tmp_path / "mirror"
    d.mkdir()
    r, lines = detach.mirror_probe("box-m", str(job), d)
    assert r.returncode == 0 and lines[-1] == "running 1", lines
    assert (d / "job.remote.log").read_text() == "early\nlate\n", "the tail was taken before the verdict"
