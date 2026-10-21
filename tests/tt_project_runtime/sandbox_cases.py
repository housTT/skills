# Process sandbox tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp sandbox` and check isolation tests here.
from __future__ import annotations


def _live_sandbox(monkeypatch):
    """A real sandbox for this test, nested when the suite itself runs in one; skips where the host
    cannot create user namespaces."""
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    pre, why = sandbox.status()
    if not pre:
        pytest.skip(f"this host allows no process sandbox: {why}")
    return sandbox


def _no_sandbox_tools(monkeypatch):
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    which = shutil.which
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, *a, **k: None if name in ("unshare", "bwrap") else which(name, *a, **k))
    return sandbox


def test_sandbox_hides_an_outside_process_and_a_kill_of_its_pid_reaches_nothing(monkeypatch):
    sandbox = _live_sandbox(monkeypatch)
    sentinel = subprocess.Popen(["sleep", "60"])
    try:
        assert sentinel.pid > 3   # never one of the namespace's own first pids
        probe = (f"import os\np = {sentinel.pid}\nprint(os.path.exists(f'/proc/{{p}}'))\n"
                 "try:\n    os.kill(p, 9)\n    print('signalled')\nexcept ProcessLookupError:\n    print('no such process')\n")
        r = subprocess.run(sandbox.wrap([sys.executable, "-c", probe]), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == ["False", "no such process"]
        assert sentinel.poll() is None   # still alive
    finally:
        sentinel.kill()
        sentinel.wait()


def test_sandbox_keeps_working_dir_environment_and_exit_code(tmp_path, monkeypatch):
    sandbox = _live_sandbox(monkeypatch)
    r = subprocess.run(sandbox.wrap(["sh", "-c", 'pwd; echo "$TTP_SANDBOX_TEST"; exit 7']), cwd=tmp_path,
                       env={**os.environ, "TTP_SANDBOX_TEST": "kept"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 7 and r.stdout.splitlines() == [str(tmp_path.resolve()), "kept"]
    # A command killed by a signal exits 128 + its number, as a shell reports it.
    r = subprocess.run(sandbox.wrap(["sh", "-c", "kill -TERM $$"]), timeout=60)
    assert r.returncode == 128 + signal.SIGTERM


def test_a_timeout_ends_the_whole_namespace_with_processes_that_ignore_term(tmp_path, monkeypatch):
    """A stray background process that ignores SIGTERM and outlives its shell still ends with the
    sandbox: its file lock comes free at once."""
    import fcntl
    sandbox = _live_sandbox(monkeypatch)
    lock, ready = tmp_path / "held", tmp_path / "ready"
    holder = ("import fcntl, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              f"f = open({str(lock)!r}, 'w')\nfcntl.flock(f, fcntl.LOCK_EX)\nopen({str(ready)!r}, 'w').close()\n"
              "time.sleep(120)\n")
    script = f"{shlex.quote(sys.executable)} -c {shlex.quote(holder)} & sleep 120"
    proc = subprocess.Popen(sandbox.wrap(["sh", "-c", script]), start_new_session=True)
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        proc.kill()   # what subprocess.run(timeout=) and every runner's last resort send
        proc.wait(timeout=30)
        with open(lock, "w") as f:
            deadline = time.time() + 10
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.time() < deadline, "a process of the namespace outlived it"
                    time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_ttp_sandbox_under_timeout_ends_on_sigterm(tmp_path, monkeypatch, capsys):
    """`timeout N ttp sandbox -- <cmd>`: the TERM to ttp sandbox ends the namespace."""
    _live_sandbox(monkeypatch)
    runtime = pathlib.Path(__import__("ttp").__file__).resolve().parent.parent
    start = time.time()
    r = subprocess.run(["timeout", "2", sys.executable, "-m", "ttp", "sandbox", "--", "sh", "-c", "sleep 60 & sleep 60"],
                       env={**os.environ, "PYTHONPATH": str(runtime)}, timeout=60)
    assert r.returncode == 124 and time.time() - start < 20


def test_ttp_checks_runs_each_check_in_the_sandbox_by_default(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    _live_sandbox(monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    sentinel = subprocess.Popen(["sleep", "60"])
    try:
        cli.main(["checks", "--fresh", "--", f"test ! -e /proc/{sentinel.pid}"])
        assert json.loads((run / "checks.json").read_text())["passed"] is True
        assert "no process sandbox" not in capsys.readouterr().out
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait()


def test_check_argv_wraps_local_checks_unless_one_opts_out(tmp_path, monkeypatch):
    from ttp import push, pushpass, sandbox
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare", "--pid"], ""))
    plain = push.check_argv("make a", tmp_path)
    assert plain[:2] == ["/sb/unshare", "--pid"] and plain[-1] == "make a"
    assert sandbox.INIT.name in " ".join(plain)
    checks = push.check_list([{"run": "make b", "sandbox": False}, "make c", {"run": "make d", "host": "box1"}])
    off, on, remote = checks
    assert off.sandbox is False and off.config() == {"run": "make b", "sandbox": False} and on.config() == "make c"
    assert push.check_argv(off, tmp_path)[0] != "/sb/unshare" and push.check_argv(off, tmp_path)[-1] == "make b"
    assert push.check_argv(remote, tmp_path)[0] != "/sb/unshare"   # ssh runs it on another machine
    assert push.check_argv(on, tmp_path, isolate=False)[0] != "/sb/unshare"
    # Already in a sandbox: run as it is, never nested.
    monkeypatch.setattr(sandbox, "inside", lambda: True)
    assert push.check_argv("make a", tmp_path)[0] != "/sb/unshare"
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    # The opt-out is reported per check; a run where every local check is sandboxed says nothing.
    assert push.sandbox_lines(checks) == [f"make b: {sandbox.notice(False)}"]
    assert push.sandbox_lines([on, remote]) == []
    # A pass recorded sandboxed or not is the same pass.
    monkeypatch.setattr(sandbox, "status", lambda: (None, "off"))
    eid = pushpass.env_id(tmp_path, "make c", dict(os.environ), "")
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare"], ""))
    assert pushpass.env_id(tmp_path, "make c", dict(os.environ), "") == eid
    # Only a boolean opts out.
    assert push.check_problems([{"run": "make b", "sandbox": "no"}])


def test_without_a_sandbox_tool_checks_run_and_say_why(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    sandbox = _no_sandbox_tools(monkeypatch)
    pre, why = sandbox.status()
    assert pre is None and "neither unshare (util-linux) nor bwrap (bubblewrap) is installed" in why
    assert sandbox.wrap(["true"]) == ["true"]
    assert push.sandbox_lines(["make a"]) == [sandbox.notice()] and why in sandbox.notice()
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    cli.main(["checks", "--fresh", "--", "true"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    assert f"ttp checks: no process sandbox: {why}" in capsys.readouterr().out
    assert "no process sandbox" in (run / "checks.log").read_text()


def test_a_blocked_user_namespace_names_the_setting_that_allows_it(tmp_path, monkeypatch):
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "unshare"
    fake.write_text("#!/bin/sh\necho 'unshare: write failed /proc/self/uid_map: Operation not permitted' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, *a, **k: str(fake) if name == "unshare" else None)
    sysctl = tmp_path / "sys"
    (sysctl / "kernel").mkdir(parents=True)
    (sysctl / "kernel" / "apparmor_restrict_unprivileged_userns").write_text("1\n")
    monkeypatch.setattr(sandbox, "SYSCTL_DIR", sysctl)
    pre, why = sandbox.status()
    assert pre is None
    assert why.startswith("sysctl kernel.apparmor_restrict_unprivileged_userns=1 turns off unprivileged user namespaces")
    assert "set it to 0" in why and "Operation not permitted" in why
    # Probed once per process (and PATH): the next caller gets the same answer without a new probe.
    fake.unlink()
    assert sandbox.status() == (None, why)


def test_ttp_checks_reports_a_push_check_that_opts_out_of_the_sandbox(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, sandbox
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setattr(sandbox, "status", lambda: (None, "unused"))
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.set_config("delivery.push_checks", [{"run": "true", "sandbox": False}])
    cli.main(["checks", "--fresh"])
    out = capsys.readouterr().out
    assert "ttp checks: true: sandbox off (\"sandbox\": false)" in out and "no process sandbox" not in out
    assert json.loads((run / "checks.json").read_text())["passed"] is True


def test_the_tool_hook_sees_the_command_inside_ttp_sandbox():
    from ttp import hook
    assert hook._strip_prefix("ttp sandbox -- pytest -q tests".split()) == ["pytest", "-q", "tests"]
