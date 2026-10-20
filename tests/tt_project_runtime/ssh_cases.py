# ssh command tests (sshcmd.py and its callers) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new ssh command tests here.
from __future__ import annotations

_EVIL_DIR = "/srv/p;touch /tmp/pwned"


def _host_and_cmd(argv: list) -> tuple[str, str]:
    """(the host, the remote command) of an ssh argv; asserts '--' comes right before the host."""
    i = argv.index("--")
    return argv[i + 1], argv[i + 2]


def test_a_remote_folder_with_shell_characters_is_quoted_in_every_remote_command(env, monkeypatch, capsys):
    import shlex
    from types import SimpleNamespace
    from ttp import cli, notifier, sshcmd, tunnel, weblink
    entry = {"host": "host-a", "dir": _EVIL_DIR}
    want = shlex.quote(f"{_EVIL_DIR}/tt-project/harness/bin/ttp")
    # cli: every command forwarded to the project there
    host, cmd = _host_and_cmd(cli._ssh_cmd(entry, ["status", "demo"]))
    assert host == "host-a" and cmd.startswith(want + " ") and shlex.split(cmd)[0] == f"{_EVIL_DIR}/tt-project/harness/bin/ttp"
    # notifier: the minute alert poll
    seen = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    assert notifier._remote_alerts(entry, "demo", 0, "high") == ([], True, None)
    host, cmd = _host_and_cmd(seen[-1])
    assert host == "host-a" and cmd.startswith(want + " ") and len(shlex.split(cmd.split(" --health")[0])) == 8, cmd
    # weblink: the daemon restart when the kept tunnel's link does not answer
    seen.clear()
    monkeypatch.setattr(weblink, "verify", lambda url, name: ("down", "no answer"))
    monkeypatch.setattr(tunnel, "installed", lambda name: {"host": "host-a"})
    monkeypatch.setattr(tunnel, "keep", lambda *a, **k: (18800, "kept"))
    monkeypatch.setattr(tunnel, "restart", lambda name: "restarted")
    monkeypatch.setattr(weblink.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    weblink.remote("demo", entry, 18700, "tok")
    assert [_host_and_cmd(a)[0] for a in seen] == ["host-a", "host-a"], seen   # the ssh check, then the restart
    host, cmd = _host_and_cmd(seen[-1])
    assert cmd == f"{want} restart demo", cmd
    # cli web: asking the daemon there for its link
    seen.clear()
    monkeypatch.setattr(cli, "resolve", lambda name: (None, entry))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: seen.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "web app: http://127.0.0.1:18700/#token=abc123\n", ""))
    monkeypatch.setattr(tunnel, "installed", lambda name: None)
    cli.main(["web", "demo"])
    host, cmd = _host_and_cmd(seen[0])
    assert cmd == f"{want} web demo", cmd
    assert sshcmd.remote_cmd(entry, ["x"]) == f"{want} x"


def test_an_ssh_host_starting_with_a_dash_is_refused_everywhere(env, monkeypatch, capsys):
    from ttp import cli, notifier, sshcmd, tunnel, weblink
    entry = {"host": "-oProxyCommand=evil", "dir": "/srv/p"}
    for bad in ("-oFoo", "", "-"):
        with pytest.raises(sshcmd.BadHost):
            sshcmd.check_host(bad)
    assert sshcmd.check_host("host-a") == "host-a"
    with pytest.raises(sshcmd.BadHost):
        sshcmd.host({"host": "host-a", "ssh": "-oFoo"})   # the ssh alias wins, and is checked too
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"ssh ran: {a}"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail(f"ssh ran: {a}"))
    with pytest.raises(SystemExit) as e:
        cli._ssh_cmd(entry, ["status"])
    assert e.value.code == 2 and "refusing ssh host" in capsys.readouterr().err
    with pytest.raises(sshcmd.BadHost):
        tunnel.ssh_argv("-oFoo", 18800, 18700)
    monkeypatch.setattr(tunnel, "keep", lambda *a, **k: pytest.fail("no tunnel to a refused host"))
    line, rc = weblink.remote("demo", entry, 18700, "tok")
    assert rc == weblink.UNVERIFIED and "refusing ssh host" in line, line
    assert notifier._remote_alerts(entry, "demo", 0, "high") == (None, False, None)
    monkeypatch.setattr(cli, "resolve", lambda name: (None, entry))
    with pytest.raises(SystemExit):
        cli.main(["web", "demo"])
    assert "refusing ssh host" in capsys.readouterr().err


def test_chat_log_markers_with_shell_characters_or_a_dash_host_are_not_matched(env, monkeypatch, tmp_path, capsys):
    from ttp import cli
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    logs = tmp_path / "userhome" / ".claude" / "projects" / "x"
    logs.mkdir(parents=True)
    (logs / "a.jsonl").write_text('{"text": "tt-project://demo@host-a:/srv/p;touch /tmp/x"}\n'
                                  '{"text": "tt-project://demo@-oFoo:/srv/q"}\n'
                                  '{"text": "tt-project://demo@host-b:~/w/demo-1.x/sub_dir"}\n'
                                  '{"text": "tt-project://demo@host-c:/srv/$(id)"}\n')
    hits = cli.search_transcripts("demo")
    # '/srv/p;touch' stops at ';' and '/srv/$(id)' at '$': only the safe prefix is a folder
    assert {(h["host"], h["dir"]) for h in hits} == {("host-a", "/srv/p"), ("host-c", "/srv/")}, hits
    # a '~' folder is not absolute, so it was never a marker
    assert all(";" not in h["dir"] and "$" not in h["dir"] for h in hits)


def test_resolve_says_which_host_and_folder_a_chat_log_hit_registers(env, monkeypatch, capsys):
    from ttp import cli
    seen = "/logs/session.jsonl"
    monkeypatch.setattr(cli, "local_project", lambda name: None)
    monkeypatch.setattr(cli, "remote_entry", lambda name: None)
    monkeypatch.setattr(cli, "search_transcripts", lambda name: [{"host": "host-a", "dir": "/srv/p", "seen_in": seen, "mtime": 1}])
    registered = []
    monkeypatch.setattr(cli, "register", lambda name, entry: registered.append((name, entry)))
    p, entry = cli.resolve("demo")
    err = capsys.readouterr().err
    assert p is None and entry == {"host": "host-a", "dir": "/srv/p"} and registered == [("demo", entry)]
    assert "registered demo at host-a:/srv/p" in err and seen in err, err


def test_a_kept_tunnel_unit_keeps_percent_and_dollar_literal(env, tmp_path, monkeypatch):
    from ttp import tunnel
    argv = tunnel.ssh_argv("host-a", 18800, 18700, ssh="/opt/ssh%i/$HOME/ssh")
    unit = tunnel.systemd_unit("demo", argv)
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "/opt/ssh%%i/$$HOME/ssh" in line and "%i" not in line.replace("%%", ""), line
    assert "$HOME" not in line.replace("$$", ""), line
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    f = tunnel.service_file("demo", "linux")
    f.parent.mkdir(parents=True)
    f.write_text(unit)
    got = tunnel.installed("demo", "linux")
    assert got["host"] == "host-a" and got["local"] == 18800 and got["remote"] == 18700 and got["own"], got
