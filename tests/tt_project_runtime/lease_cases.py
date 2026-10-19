# Project lease tests (lease.py, failover step L1) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new lease tests here.
from __future__ import annotations


def _lease_remote(tmp_path) -> Path:
    remote = tmp_path / "lease-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return remote


def _leases(tmp_path, remote, clock, ttl=100.0):
    """A factory of Lease objects on one store, each a host with its own git dir and state file."""
    from ttp import lease

    def mk(host, base="/projects/demo"):
        return lease.Lease(lease.GitStore(str(remote), "demo", tmp_path / f"{host}-{len(base)}.git"), "demo",
                           host=host, base=base, ttl=ttl, clock=clock, state_file=tmp_path / f"{host}.json")
    return mk


def test_lease_acquire_contention_expiry_and_takeover(env, tmp_path):
    """One holder at a time. The holder counts itself the holder only until 0.8 ttl after its last
    renewal; another host takes the lease only after it saw the same record unchanged 1.25 ttl on its
    own clock, at epoch + 1, and the old holder then learns it lost it. A renewal keeps the epoch."""
    t = [1000.0]
    mk = _leases(tmp_path, _lease_remote(tmp_path), lambda: t[0])
    a, b = mk("host-a"), mk("host-b")
    assert a.step() == "acquired" and a.held() and a.epoch == 1
    assert b.step() == "watching" and not b.held() and b.snapshot()["holder_host"] == "host-a"
    t[0] += 10
    assert a.step() == "kept", "no store call between renewals (every ttl/5)"
    t[0] += 15
    assert a.step() == "renewed" and a.epoch == 1, "a renewal never moves the epoch"
    assert b.step() == "watching"
    # host-a stops renewing (crash or partition): it stops counting itself the holder at 0.8 ttl...
    t[0] += 79
    assert a.held()
    t[0] += 2
    assert not a.held()
    # ...and host-b, which saw the renewal 25 s in, may take the lease only 1.25 ttl after that.
    assert b.step() == "watching", "81 s unchanged is not yet 1.25 ttl"
    t[0] += 43
    assert b.step() == "watching"
    t[0] += 21
    assert b.step() == "taken" and b.held() and b.epoch == 2
    rec = b.rec
    assert rec["holder_host"] == "host-b" and rec["transitions"] == 2 and rec["role"] == "primary"
    assert a.step() in ("lost", "watching") and not a.held() and a.snapshot()["holder_host"] == "host-b"
    assert a.max_epoch == 2, "the old holder has seen the higher epoch (the fencing token)"


def test_lease_holder_that_lost_a_race_stops_at_once(env, tmp_path):
    """A renewal whose compare-and-swap loses (someone wrote in between) ends the claim right away."""
    from ttp import lease
    t = [0.0]
    mk = _leases(tmp_path, _lease_remote(tmp_path), lambda: t[0])
    a, b = mk("host-a"), mk("host-b")
    assert a.step() == "acquired"
    rec, ver = b.store.read()
    assert b.store.cas(ver, {**rec, "holder_host": "host-b", "epoch": 2})
    assert not b.store.cas(ver, {**rec, "epoch": 3}), "a stale version never writes"
    t[0] += 30
    assert a.step() == "lost" and not a.held()
    assert isinstance(lease.Unreachable("x"), Exception)


def test_lease_renews_after_a_restart_and_after_sleep(env, tmp_path):
    """A daemon restarted on the holder's host (same project folder) renews its record at once at the
    same epoch: the earlier daemon is dead, daemon.lock is one per host. A second project folder on
    the same host is another holder. After a sleep the lease counts as lapsed until it is renewed."""
    t = [0.0]
    mk = _leases(tmp_path, _lease_remote(tmp_path), lambda: t[0])
    a = mk("host-a")
    assert a.step() == "acquired" and a.epoch == 1
    again = mk("host-a")
    assert not again.held(), "a fresh daemon holds nothing before its first renewal"
    assert again.step() == "resumed" and again.held() and again.epoch == 1
    other = mk("host-a", base="/projects/copy")
    assert other.step() == "watching"
    again.lapse()
    assert not again.held()
    assert again.step() == "renewed" and again.held()


def test_lease_release_lets_the_next_holder_take_it_at_once(env, tmp_path):
    t = [0.0]
    mk = _leases(tmp_path, _lease_remote(tmp_path), lambda: t[0])
    a, b = mk("host-a"), mk("host-b")
    a.step()
    assert not b.release(), "only the holder releases"
    assert a.release() and not a.held()
    assert b.step() == "acquired" and b.epoch == 2


def test_lease_store_unreachable_lapses_the_holder_and_nobody_takes_it(env, tmp_path):
    import shutil as sh
    t = [0.0]
    remote = _lease_remote(tmp_path)
    mk = _leases(tmp_path, remote, lambda: t[0])
    a = mk("host-a")
    assert a.step() == "acquired"
    sh.move(str(remote), str(tmp_path / "gone.git"))
    t[0] += 30
    assert a.step() == "unreachable" and a.held() and a.error
    t[0] += 51
    assert not a.held(), "the holder self-expires at 0.8 ttl without a renewal"
    b = mk("host-b")
    assert b.step() == "unreachable" and not b.held()


def test_lease_refuses_the_plugins_own_repo_and_the_delivery_remote(env, capsys):
    """Lease refs never go to the plugin's own repository (any URL form or remote name), nor to the
    project's delivery remote unless failover.allow_delivery_remote; `ttp lease check` says why."""
    from ttp import cli, lease
    p = make(env)
    subprocess.run(["git", "-C", str(p.root), "remote", "add", "origin", "git@example.com:org/code.git"], check=True)
    subprocess.run(["git", "-C", str(p.root), "remote", "add", "upstream",
                    "https://github.com/tenstorrent/skills.git"], check=True)
    # An insteadOf alias is rewritten the way git rewrites it before the comparison.
    subprocess.run(["git", "-C", str(p.root), "config", "url.https://github.com/tenstorrent/.insteadOf", "tt:"],
                   check=True)
    for own in ("https://github.com/tenstorrent/skills", "git@github.com:tenstorrent/skills.git",
                "ssh://git@github.com/Tenstorrent/skills.git/", "upstream", "github.com:tenstorrent/skills",
                "gh:tenstorrent/skills", "gh:/Tenstorrent/Skills.git", "tt:skills"):
        cfg = {"id": "x", "failover": {"lease_remote": own, "allow_delivery_remote": True}}
        with pytest.raises(lease.Refused, match="plugin's own repository"):
            lease.resolve_remote(p, cfg)
    for code in ("origin", "ssh://git@example.com/org/code", "example.com:org/code", "example.com:org/code.git"):
        with pytest.raises(lease.Refused, match="delivery remote"):
            lease.resolve_remote(p, {"failover": {"lease_remote": code}})
        assert lease.resolve_remote(p, {"failover": {"lease_remote": code, "allow_delivery_remote": True}})
    assert lease.resolve_remote(p, {"failover": {"lease_remote": "git@example.com:org/leases.git"}}) \
        == "git@example.com:org/leases.git"
    cfg = json.loads(p.config_path.read_text())
    cfg["failover"] = {"lease_remote": "https://github.com/tenstorrent/skills"}
    p.config_path.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit) as e:
        cli.main(["lease", "check", "demo"])
    assert e.value.code == 1 and "refused" in capsys.readouterr().out
    # The daemon fails closed on a refused store: no dispatch, one alert saying why.
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.tend_lease()
    assert not d.lease_held()
    assert p.db.one("SELECT text FROM events WHERE kind='lease'")["text"].startswith("project lease: failover")
    # ...and `ttp status` says the store is refused, not that the lease was never checked.
    line = cli.status_text(p)
    assert "lease: refused (failover.lease_remote https://github.com/tenstorrent/skills is the" in line
    assert ": no dispatch" in line and "not checked yet" not in line


def test_lease_store_rejecting_the_lease_ref_is_an_error_not_a_lost_race(env, tmp_path, monkeypatch):
    """A store whose hook or ruleset rejects refs/ttp/* is a store error: the write raises with the
    remote's reason, the daemon alerts once and `ttp status` shows it, never 'free' with no error.
    Only a stale-info rejection of the lease ref is a lost race."""
    from ttp import cli, lease
    remote = _lease_remote(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text('#!/bin/sh\nwhile read o n r; do case "$r" in refs/ttp/*) '
                    'echo "refs/ttp/* is not allowed here" >&2; exit 1;; esac; done\n')
    hook.chmod(0o755)
    store = lease.GitStore(str(remote), "demo", tmp_path / "probe.git")
    with pytest.raises(lease.Rejected, match="remote rejected"):
        store.cas("", {"epoch": 1, "holder_host": "h"})
    t = [0.0]
    a = _leases(tmp_path, remote, lambda: t[0])("host-a")
    assert a.step() == "rejected" and not a.held() and "remote rejected" in a.error
    p = make(env)
    cfg = json.loads(p.config_path.read_text())
    cfg["failover"] = {"lease_remote": str(remote), "lease_ttl_s": 100}
    p.config_path.write_text(json.dumps(cfg))
    real = lease.for_project
    monkeypatch.setattr(lease, "for_project", lambda p, cfg, **kw: real(p, cfg, clock=lambda: t[0]))
    calls: list[str] = []
    d = _hold_daemon(p.base, calls)
    d.tick()
    t[0] += 30
    d.tick()
    assert calls == [] and not d.lease_held()
    line = cli.status_text(p)
    assert "lease: store rejected the lease write (" in line and "remote rejected" in line and ": no dispatch" in line
    assert "free" not in line
    evs = p.db.q("SELECT text FROM events WHERE kind='lease'")
    assert len(evs) == 1 and "rejected" in evs[0]["text"]
    alerts = p.db.q("SELECT text FROM messages WHERE kind='alert'")
    assert len(alerts) == 1 and "remote rejected" in alerts[0]["text"]


def test_daemon_without_a_lease_store_behaves_as_today(env):
    from ttp import cli
    p = make(env)
    calls: list[str] = []
    d = _hold_daemon(p.base, calls)
    d.tick()
    assert "dispatch" in calls and "push" in calls and "coordinate" in calls
    assert d.lease_held() and d._lease is None and not (p.state / "lease.json").exists()
    assert "lease:" not in cli.status_text(p)
    assert not p.db.one("SELECT id FROM events WHERE kind='lease'")


def test_daemon_dispatches_only_while_it_holds_the_lease(env, tmp_path, monkeypatch):
    """Another host holds the lease: the tick skips coordinator turns, dispatch and pushes, says so once,
    and `ttp status` shows who holds it. Once the record stands still 1.25 ttl, this host takes it."""
    from ttp import cli, lease
    p = make(env)
    remote = _lease_remote(tmp_path)
    cfg = json.loads(p.config_path.read_text())
    cfg["failover"] = {"lease_remote": str(remote), "lease_ttl_s": 100}
    p.config_path.write_text(json.dumps(cfg))
    t = [5000.0]
    other = lease.Lease(lease.GitStore(str(remote), lease.identity(p, p.config()), tmp_path / "other.git"),
                        lease.identity(p, p.config()), host="otherhost", base="/elsewhere", ttl=100, clock=lambda: t[0])
    assert other.step() == "acquired"
    real = lease.for_project
    monkeypatch.setattr(lease, "for_project", lambda p, cfg, **kw: real(p, cfg, clock=lambda: t[0]))
    calls: list[str] = []
    d = _hold_daemon(p.base, calls)
    d.tick()
    assert calls == [] and not d.lease_held()
    line = cli.status_text(p)
    assert "lease: NOT held here (held by otherhost, primary, epoch 1" in line and "no dispatch" in line
    evs = p.db.q("SELECT text FROM events WHERE kind='lease'")
    assert len(evs) == 1 and "lease held by otherhost" in evs[0]["text"]
    d.tick()
    assert len(p.db.q("SELECT id FROM events WHERE kind='lease'")) == 1, "one event per change of state"
    t[0] += 130
    calls.clear()
    d.tick()
    assert d.lease_held() and d._lease.epoch == 2 and "dispatch" in calls and "coordinate" in calls
    assert "lease: held here (epoch 2" in cli.status_text(p)
    assert json.loads((p.state / "lease.json").read_text())["held"] is True
