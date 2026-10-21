# Remote check host tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file. A fake
# `ssh` on PATH drops its -o options and the host and runs the last argument with `sh -c` here, so
# no network is needed.
from __future__ import annotations

_RC_FAKE_SSH = r'''#!/bin/sh
# A fake ssh: drops -o options and the host, runs the last argument with sh -c. Host "down" is
# unreachable; FAKE_SSH_FAIL=<text> exits 255 at once on a script holding it (reading nothing);
# FAKE_SSH_DROP=<text> runs such a script, then drops the connection before its exit-code marker.
while [ "$1" = "-o" ]; do shift 2; done
host=$1
shift
if [ "$host" = down ]; then echo "ssh: connect to host down port 22: Connection refused" >&2; exit 255; fi
[ -n "$FAKE_REMOTE_TMP" ] && export TMPDIR="$FAKE_REMOTE_TMP"
case "$1" in *"${FAKE_SSH_FAIL:-@none@}"*) echo "ssh: connection to $host lost" >&2; exit 255;; esac
case "$1" in *"${FAKE_SSH_DROP:-@none@}"*) sh -c "$1" | sed '/ttp-remote-rc:/d'; exit 255;; esac
exec sh -c "$1"
'''


def _rc_fake_ssh(tmp_path):
    """A bin folder holding the fake ssh, and the folder its remote temp dirs go to."""
    bin_, remote = tmp_path / "fakessh", tmp_path / "remote"
    bin_.mkdir(exist_ok=True)
    remote.mkdir(exist_ok=True)
    ssh = bin_ / "ssh"
    ssh.write_text(_RC_FAKE_SSH)
    ssh.chmod(0o755)
    return bin_, remote


def _rc_repo(tmp_path, big=False):
    repo = tmp_path / "work"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    if big:   # far more than a pipe buffer: git archive is still writing when ssh drops
        (repo / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    return repo


def _rc_run(tmp_path, repo, cmd, host="box1", env=None, wait=True, **fake):
    """remotecheck run as callers run it (push.check_argv's argv) in `repo` against the fake ssh:
    (exit code, output); with wait=False the Popen."""
    from ttp import remotecheck
    bin_, remote = _rc_fake_ssh(tmp_path)
    penv = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_REMOTE_TMP": str(remote), **fake}
    proc = subprocess.Popen(remotecheck.argv(cmd, host, env), cwd=repo, env=penv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    if not wait:
        return proc
    out = proc.communicate(timeout=60)[0]
    return proc.returncode, out


def _rc_left(tmp_path):
    return sorted(p.name for p in (tmp_path / "remote").glob("ttp-check.*"))


def test_a_remote_check_runs_on_a_git_checkout_of_exactly_head_and_cleans_up(tmp_path):
    """The command runs in a git work tree whose tree is HEAD's (not the uncommitted edit, not the
    untracked file), with the check's env exported; its exit code is the check's (75 as 1), and the
    temp dir is gone after a pass and a failure."""
    repo = _rc_repo(tmp_path)
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True,
                          text=True).stdout.strip()
    (repo / "a.txt").write_text("uncommitted\n")
    (repo / "untracked.txt").write_text("x\n")
    out = tmp_path / "seen"
    out.mkdir()
    cmd = (f"git rev-parse --is-inside-work-tree > {out}/inside; git rev-parse HEAD^{{tree}} > {out}/tree; "
           f"git status --porcelain > {out}/status; cat a.txt > {out}/a; test ! -e untracked.txt; "
           f"printf %s \"$GREETING\" > {out}/env; pwd > {out}/pwd; echo hello")
    rc, said = _rc_run(tmp_path, repo, cmd, env={"GREETING": 'hi "there"'})
    assert rc == 0, said
    assert (out / "inside").read_text().strip() == "true"
    assert (out / "tree").read_text().strip() == tree, "the exact head, as git archive copies it"
    assert (out / "status").read_text() == "" and (out / "a").read_text() == "a\n"
    assert (out / "env").read_text() == 'hi "there"'
    assert "/ttp-check." in (out / "pwd").read_text() and "hello" in said and "ttp-remote-rc" not in said
    assert _rc_left(tmp_path) == []
    assert _rc_run(tmp_path, repo, "exit 3")[0] == 3
    rc, said = _rc_run(tmp_path, repo, "exit 75")
    assert rc == 1 and "reported as 1" in said
    assert _rc_left(tmp_path) == []


def test_ssh_failing_at_any_step_of_a_remote_check_is_an_environment_fault(tmp_path, monkeypatch):
    """ssh exiting 255 when making the temp dir, during the copy (also of a tree bigger than a pipe
    buffer, where git archive dies of SIGPIPE) or mid-run exits 75, never the change's 1 or 2."""
    from ttp import remotecheck
    repo = _rc_repo(tmp_path, big=True)
    for fake in ({"FAKE_SSH_FAIL": "mktemp"}, {"FAKE_SSH_FAIL": "tar -xf"}, {"FAKE_SSH_DROP": "ttp-remote-rc"}):
        rc, said = _rc_run(tmp_path, repo, "true", **fake)
        assert rc == remotecheck.FAULT_RC, (fake, said)
        assert f"{remotecheck.SIG}box1" in said and "git archive" not in said, said
    assert _rc_run(tmp_path, repo, "true", host="down")[0] == remotecheck.FAULT_RC
    assert _rc_left(tmp_path) == []
    bin_, _ = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    assert remotecheck.reachable("box1") == ""
    assert "cannot reach down" in remotecheck.reachable("down")


def _rc_gone(pid, within=10.0):
    end = time.time() + within
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if pathlib.Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                return True
        except (OSError, IndexError):
            pass
        time.sleep(0.05)
    return False


def test_a_stopped_remote_check_stops_its_command_on_the_host(tmp_path):
    """SIGTERM to the local side (a timeout, a `ttp checks` stop, a queue stop) stops the command where
    it runs (ssh without a tty signals nothing) and removes its temp dir."""
    repo = _rc_repo(tmp_path)
    started, marker = tmp_path / "started", tmp_path / "marker"
    proc = _rc_run(tmp_path, repo, f"sleep 30 & echo $! > {started}.tmp; mv {started}.tmp {started}; wait $!; "
                                   f"echo alive > {marker}", wait=False)
    try:
        end = time.time() + 30
        while not started.exists() and time.time() < end:
            time.sleep(0.05)
        assert started.exists(), proc.stdout.read() if proc.poll() is not None else "never started"
        sleeper = int(started.read_text())
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 143
    assert _rc_gone(sleeper), "the command's process group was stopped"
    time.sleep(0.3)
    assert not marker.exists() and _rc_left(tmp_path) == []


def test_ttp_checks_on_a_check_host_names_it_and_waits_when_it_cannot_reach_it(env, tmp_path, monkeypatch):
    """delivery.checks_host: `ttp checks` runs there, and checks.json and checks.log say so. A host ssh
    cannot reach (found by the probe, or mid-run) exits 75 with env_fault env:check-host:<host>, records
    no pass, and counts nothing against the change."""
    from ttp import cli, envfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    bin_, remote = _rc_fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    log = tmp_path / "where.log"
    cmd = f"pwd >> {log}"
    p.set_config("delivery.push_checks", [cmd])
    p.set_config("delivery.checks_host", "box1")
    cli.main(["checks", "--fresh"])
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["hosts"] == {cmd: "box1"}, rec
    assert f"$ {cmd} (on box1)" in (run / "checks.log").read_text()
    assert "/ttp-check." in log.read_text() and _rc_left(tmp_path) == []
    tasks = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    for host, fake in (("down", {}), ("box1", {"FAKE_SSH_FAIL": "mktemp"})):
        for k, v in fake.items():
            monkeypatch.setenv(k, v)
        p.set_config("delivery.checks_host", host)
        (run / "checks.json").unlink()
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh"])
        assert e.value.code == 75, host
        rec = json.loads((run / "checks.json").read_text())
        assert rec["passed"] is False and rec["env_fault"] == f"env:check-host:{host}", rec
        assert not (run / "check_failure.json").exists()
        assert f"env:check-host:{host}" in envfault.open_faults(p)
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == tasks, "no review or fix task"
    assert len(log.read_text().splitlines()) == 1, "the unreachable host ran nothing"


def test_ttp_push_runs_its_checks_on_the_check_host_and_waits_when_ssh_fails(env, monkeypatch):
    from ttp import envfault, push
    log = env["tmp"] / "where.log"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"pwd >> {log}"])
    p.set_config("delivery.checks_host", "box1")
    bin_, remote = _rc_fake_ssh(env["tmp"])
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    assert "/ttp-check." in log.read_text()
    _commit(repo, "more.txt", "more\n")
    monkeypatch.setenv("FAKE_SSH_FAIL", "mktemp")   # reachable for the probe, gone for the check
    assert _ttp_push() == push.BUSY
    assert "env:check-host:box1" in envfault.open_faults(p)
    monkeypatch.delenv("FAKE_SSH_FAIL")
    p.set_config("delivery.checks_host", "down")
    assert _ttp_push() == push.BUSY
    assert len(log.read_text().splitlines()) == 1


def test_a_push_batch_runs_on_the_check_host_and_holds_on_its_fault(env, monkeypatch):
    """The push queue's batch runs the remote argv; a check host that drops is envfault.Fault (the
    batch ends env_fault, entries requeued), not check_failed."""
    from ttp import envfault, push
    log = env["tmp"] / "where.log"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"pwd >> {log}"])
    p.set_config("delivery.checks_host", "box1")
    bin_, remote = _rc_fake_ssh(env["tmp"])
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_TMP", str(remote))
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]), monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert "/ttp-check." in log.read_text()
    monkeypatch.setenv("FAKE_SSH_FAIL", "mktemp")
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e2", {"plugins/p/f2.txt": "2\n"})], bid="b2"), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "env_fault" and _statuses(m) == ["requeued"], m
    assert envfault.open_faults(p)["env:check-host:box1"]["queue"] is True


def test_check_host_settings_are_validated_and_local_keeps_a_check_here():
    from ttp import coordinator, project, push
    assert not project.config_problems({"delivery": {"checks_host": "build-box.example"}})
    assert any("delivery.checks_host" in x for x in project.config_problems({"delivery": {"checks_host": "a b"}}))
    assert any("delivery.checks_host" in x for x in project.config_problems({"delivery": {"checks_host": 3}}))
    assert push._form_problem({"run": "make test", "host": "box1", "env": {"PATH": "$HOME/bin:$PATH"}}) is None
    assert push._form_problem({"run": "make test", "host": "local"}) is None
    for bad in ({"host": "a;b"}, {"host": ""}, {"env": {"1X": "v"}}, {"env": {"X": 1}}, {"env": ["X=1"]}):
        assert push._form_problem({"run": "make test", **bad}), bad
    settable = coordinator.USER_SETTABLE["delivery.checks_host"]
    assert settable("box1") == "box1" and settable("") == ""
    with pytest.raises(ValueError):
        settable("a b")
    assert push.checks_of([{"run": "make test", "host": "box1", "env": {"A": "1"}}]) == [
        {"run": "make test", "host": "box1", "env": {"A": "1"}}]
    here, there = push.with_host(push.check_list([{"run": "make a", "host": "local"}, "make b"]), "box1")
    assert push.remote_host(here) == "" and push.where(here) == ""
    assert "--host" not in push.check_argv(here) and push.check_argv(here)[-1] == "make a"
    argv = push.check_argv(there)
    assert argv[argv.index("--host") + 1] == "box1" and argv[-1] == "make b" and push.where(there) == " (on box1)"
    assert push.with_host(push.check_list(["make c"]), "a b") == ["make c"] and not push.remote_host(
        push.with_host(push.check_list(["make c"]), "a b")[0])
