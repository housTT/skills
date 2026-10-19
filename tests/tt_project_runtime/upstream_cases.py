# Upstream-notes tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new upstream tests here, not there.
from __future__ import annotations


def _fwd_state(target):
    from ttp import upstream
    return json.loads(upstream.forward_path().read_text())["targets"][target]


@pytest.mark.parametrize("darwin", [True, False])
def test_repeated_network_unreachable_forward_failures_read_as_a_macos_local_network_block(env, monkeypatch, darwin):
    """The daemon's forward records how often and since when it failed; on macOS, LOCAL_NET_FAILS
    'Network is unreachable' failures in a row are a suspected Local Network block, and `ttp doctor`
    names the fix with the daemon's python. Another error, or a send that goes through, ends it."""
    from ttp import upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    real = upstream._ssh_receive
    monkeypatch.setattr(upstream, "_darwin", lambda: darwin)
    upstream.set_forward_to(["example-host"])
    upstream.append("demo", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    fake["mode"].write_text("down")
    now = time.time()
    for i in range(upstream.LOCAL_NET_FAILS):
        assert upstream.forward(now + i * 7200) == 0
        st = _fwd_state("example-host")
        assert (st["fails"], st["fail_since"], st["unreachable"]) == (i + 1, now, i + 1)
        assert ("suspect" in st) == (darwin and i + 1 >= upstream.LOCAL_NET_FAILS)
    later = now + upstream.LOCAL_NET_FAILS * 7200
    lines = upstream.doctor_lines("UTC", later)
    assert len(lines) == 1 and f"failed {upstream.LOCAL_NET_FAILS}x since" in lines[0]
    py = os.path.realpath(sys.executable)
    assert (("Local Network" in lines[0]) and py in lines[0]) == darwin, lines
    if darwin:
        assert _fwd_state("example-host")["python"] == py
        assert "Local Network" in "\n".join(upstream.forward_status(later))
    # A different error ends the run of unreachable answers; the streak and its start stay.
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (None, "exit 255: Permission denied"))
    assert upstream.forward(later) == 0
    st = _fwd_state("example-host")
    assert (st["unreachable"], "suspect" in st, st["fail_since"]) == (0, False, now)
    assert "Local Network" not in "\n".join(upstream.doctor_lines("UTC", later))
    # A send that goes through clears the failure record.
    monkeypatch.setattr(upstream, "_ssh_receive", real)
    fake["mode"].write_text("ok")
    assert upstream.forward(later + 7200) == 1
    st = _fwd_state("example-host")
    assert not st["fails"] and not {"fail_since", "unreachable", "suspect", "python"} & set(st)
    assert upstream.doctor_lines("UTC", later + 7200) == []


def test_the_run_fallback_sends_a_failing_targets_notes_once_and_leaves_the_daemons_record(env, monkeypatch):
    """Past FALLBACK_AFTER_S of daemon failures, `ttp upstream --forward-now` (a run or the CLI) sends
    the pending notes through the same cursor: a lost ack, a resend and the daemon's later pass file
    each note once, and targets the daemon reaches are left to it."""
    from ttp import cli, upstream
    fake = _fake_forward_ssh(env, monkeypatch)
    monkeypatch.setattr(upstream, "_darwin", lambda: True)
    upstream.set_forward_to(["example-host", "other-host"])
    fake["mode"].write_text("ok")
    now = time.time() - 3 * 3600                            # the daemon began failing hours ago
    assert upstream.forward(now) == 0                       # both reachable, nothing yet
    upstream.append("demo", 3, [{"title": "upstream: one", "spec": "a"}, {"title": "upstream: two", "spec": "b"}])
    # Only example-host fails for the daemon.
    real = upstream._ssh_receive
    monkeypatch.setattr(upstream, "_ssh_receive", lambda t, lines, timeout: (
        (None, "exit 255: ssh: connect to host x port 22: Network is unreachable") if t == "example-host"
        else real(t, lines, timeout)))
    assert upstream.forward(now + 60) == 2                  # other-host got both
    for later in (now + 200, now + 500):
        assert upstream.forward(later) == 0
    assert not upstream.fallback_due(now + 60 + upstream.FALLBACK_AFTER_S - 1)
    assert upstream.fallback_due(now + 60 + upstream.FALLBACK_AFTER_S)
    monkeypatch.setattr(upstream, "_ssh_receive", real)
    t = now + 60 + upstream.FALLBACK_AFTER_S
    calls = len(_ssh_calls(fake))
    # The run's send loses its ack: the cursor stays and its own back-off starts, the daemon's record stays.
    fake["mode"].write_text("drop-ack")
    assert upstream.forward(t, context="run") == 0
    assert _ssh_calls(fake)[calls:] == ["example-host"], "the fallback only tries the failing target"
    st = _fwd_state("example-host")
    assert st["run_fails"] == 1 and st["fails"] == 3 and st["suspect"] == "local_network" and not st.get("cursor")
    assert not upstream.fallback_due(t + 1), "the fallback waits out its own back-off"
    fake["mode"].write_text("ok")
    cli.main(["upstream", "--forward-now"])                 # the fallback's command, at the real time
    assert sorted(n["title"] for n in _remote_inbox(fake, "example-host")) == ["upstream: one", "upstream: two"]
    st = _fwd_state("example-host")
    assert st["cursor"] == upstream.path().stat().st_size and st["run_ok"] > t and "run_fails" not in st
    assert st["fails"] == 3 and st["suspect"] == "local_network", "the daemon's failure record is its own"
    assert not upstream.fallback_due(), "nothing is pending"
    # The daemon reaches it again: nothing is sent twice, and its failure record clears.
    assert upstream.forward(time.time() + 10) == 0
    titles = [n["title"] for n in _remote_inbox(fake, "example-host")]
    assert sorted(titles) == ["upstream: one", "upstream: two"]
    assert [n["title"] for n in _remote_inbox(fake, "other-host")].count("upstream: one") == 1
    assert not _fwd_state("example-host")["fails"]


def test_note_to_and_status_warn_when_the_forward_target_has_failed_for_hours(env, monkeypatch, capsys):
    """`ttp note --to` a project on a machine the daemon has failed to reach for WARN_AFTER_S says so
    and starts the fallback; `ttp status` shows the same line. A fresh failure says nothing yet."""
    from ttp import cli, upstream
    from ttp.project import register
    p = make(env)
    _note_run(env, monkeypatch, p)
    register("example-project", {"host": "example-host", "dir": "/w/example"})
    spawned = []
    monkeypatch.setattr(upstream, "spawn_fallback", lambda: spawned.append(1) or True)
    now = time.time()
    st = {"fails": 4, "fail_since": now - 60, "error_at": now - 60, "next": now + 600, "cursor": 0,
          "last_error": "exit 255: Network is unreachable", "unreachable": 4, "suspect": "local_network",
          "python": "/opt/example/bin/python3"}
    upstream.project.write_json(upstream.forward_path(), {"targets": {"example-host": st}})
    assert _note(cli, "--to", "example-project", "first note") == 0
    out = capsys.readouterr().out
    assert "warning" not in out and spawned == []
    assert "sending upstream notes on" not in cli.status_text(p)
    st["fail_since"] = now - 3 * 3600
    upstream.project.write_json(upstream.forward_path(), {"targets": {"example-host": st}})
    assert _note(cli, "--to", "example-project", "second note") == 0
    out = capsys.readouterr().out
    assert "warning: sending upstream notes on to example-host has failed for 3.0 h (4x" in out, out
    assert "Local Network" in out and "keep working" in out
    assert spawned == [1], "the run sends the notes the daemon cannot"
    assert "sending upstream notes on to example-host has failed for 3.0 h" in cli.status_text(p)
    # A target no longer among the forward targets is not reported.
    upstream.set_forward_to(["other-host"])
    assert upstream.forward_line() == "" and upstream.failing() == {}
