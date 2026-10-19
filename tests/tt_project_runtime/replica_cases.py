# Standby state snapshot tests (replica.py) of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

import gzip


def _replica_project(env, tmp_path, keep=3):
    from ttp import replica
    p = make(env)
    dest = tmp_path / "standby"
    p.set_config("failover.standby", str(dest))
    p.set_config("failover.snapshot_keep", keep)
    return p, replica, dest / replica.identity(p, p.config())


def _replica_task(p, status, big_log=True):
    now = time.time()
    tid = p.db.x("INSERT INTO tasks(created,updated,title,status) VALUES(?,?,?,?)", (now, now, f"t {status}", status))
    tid = tid if isinstance(tid, int) else p.db.one("SELECT MAX(id) id FROM tasks")["id"]
    run_dir = p.runs / f"r{tid}"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": f"task {tid}"}))
    (run_dir / "prompt.md").write_text("prompt")
    if big_log:
        (run_dir / "output.jsonl").write_bytes(b"x" * 300_000)
    p.db.x("INSERT INTO runs(task,role,dir,status,started) VALUES(?,?,?,?,?)", (tid, "worker", str(run_dir), "ok", now))
    return tid


def _replica_harness_commit(p, name="note.md", text="x"):
    (p.harness / name).write_text(text)
    subprocess.run(["git", "-C", str(p.harness), "add", name], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p.harness), "commit", "-qm", f"edit {name}"], check=True, capture_output=True)


def test_replica_is_off_without_a_standby_and_changes_nothing(env):
    from ttp import replica
    from ttp.daemon import Daemon
    from ttp.cli import status_text
    p = make(env)
    assert replica.settings(p.config()) is None
    d = Daemon(p.base)
    d.replicate_state()
    assert d._replica_job is None and not (p.state / replica.STATE).exists()
    assert replica.snapshot(p) == {"outcome": "off"}
    assert "standby" not in status_text(p)


def test_replica_snapshot_leaves_out_run_folders_and_secrets_and_restores(env, tmp_path):
    p, replica, ident_dir = _replica_project(env, tmp_path)
    open_task = _replica_task(p, "waiting")
    done_task = _replica_task(p, "done")
    (p.state / "web.token").write_text("secret-web-token")
    p.db.set_kv("slack_bot_token", "secret-kv-token")
    p.db.set_kv("plain", 42)
    r = replica.snapshot(p)
    assert r["outcome"] == "ok" and r["seq"] == 1, r
    snap = ident_dir / "000001"
    names = {f.relative_to(snap).as_posix() for f in snap.rglob("*") if f.is_file()}
    assert f"runs/{open_task}/result.json" in names and f"runs/{open_task}/prompt.md" in names
    assert not any(n.startswith(f"runs/{done_task}/") for n in names), "only open tasks' latest runs go"
    assert not any(n.endswith("output.jsonl") for n in names), "run logs stay behind"
    assert not any("web.token" in n for n in names)
    assert {"manifest.json", "project.db.gz", "harness.bundle", "shared.json"} <= names
    raw = gzip.decompress((snap / "project.db.gz").read_bytes())
    assert b"secret-kv-token" not in raw and b"secret-web-token" not in raw
    # restore: a project folder with the database and the harness at the snapshot's head
    head = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = replica.restore(ident_dir, tmp_path / "standin")
    base = pathlib.Path(out["project_dir"])
    assert out["seq"] == 1 and out["harness_head"] == head
    assert subprocess.run(["git", "-C", str(base / "harness"), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == head
    assert (base / "harness" / "project.json").is_file()
    c = sqlite3.connect(str(base / "state" / "project.db"))
    try:
        kv = dict(c.execute("SELECT key, value FROM kv").fetchall())
        assert "slack_bot_token" not in kv and json.loads(kv["plain"]) == 42
        assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 2
    finally:
        c.close()
    assert (base / "state" / "standby-import" / "runs" / str(open_task) / "result.json").is_file()
    with pytest.raises(replica.SnapshotError, match="not empty"):
        replica.restore(ident_dir, tmp_path / "standin")


def test_replica_skips_an_unchanged_state_and_chains_harness_bundles(env, tmp_path):
    p, replica, ident_dir = _replica_project(env, tmp_path)
    assert replica.snapshot(p)["outcome"] == "ok"
    assert replica.snapshot(p)["outcome"] == "unchanged", "nothing changed: nothing shipped"
    _replica_harness_commit(p, "a.md", "one")
    r = replica.snapshot(p)
    assert r["outcome"] == "ok" and r["seq"] == 2 and not r["full"]
    m = json.loads((ident_dir / "000002" / "manifest.json").read_text())
    assert m["chain_from"] == 1 and m["harness"]["base"]
    _replica_harness_commit(p, "b.md", "two")
    assert replica.snapshot(p)["seq"] == 3
    head = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = replica.restore(ident_dir / "000003", tmp_path / "standin")
    assert out["harness_head"] == head
    assert (pathlib.Path(out["project_dir"]) / "harness" / "b.md").read_text() == "two"


def test_replica_retention_keeps_the_newest_and_their_chains(env, tmp_path):
    p, replica, ident_dir = _replica_project(env, tmp_path, keep=2)

    def have():
        return sorted(int(x.name) for x in ident_dir.iterdir() if x.name.isdigit())

    for i in range(6):
        _replica_harness_commit(p, f"f{i}.md", str(i))
        r = replica.snapshot(p)
        assert r["outcome"] == "ok" and (r["full"] or i > 0), r
        assert len(have()) <= 2 + 1, f"the newest two and the full one they build on, not {have()}"
    assert have() == [1, 5, 6], "each snapshot builds on the full #1 only, so the ones between go"
    m = json.loads((ident_dir / "000006" / "manifest.json").read_text())
    assert m["chain_from"] == 1 and not m["harness"]["full"]
    head = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = replica.restore(ident_dir, tmp_path / "standin")
    assert out["seq"] == 6 and out["harness_head"] == head
    assert all((pathlib.Path(out["project_dir"]) / "harness" / f"f{i}.md").read_text() == str(i) for i in range(6))
    st = replica.load_state(p)
    st["need_full"] = True
    replica._save_state(p, st)
    _replica_harness_commit(p, "g.md", "g")
    r = replica.snapshot(p)
    assert r["outcome"] == "ok" and r["full"] and r["kept"] == [1, 6, 7]   # #6 still needs #1
    _replica_harness_commit(p, "h.md", "h")
    r = replica.snapshot(p)
    assert r["kept"] == [7, 8]
    assert sorted(x.name for x in ident_dir.iterdir() if not x.name.startswith(".")) == ["000007", "000008"]
    v = replica.verify(p.config(), replica.identity(p, p.config()))
    assert [(x["seq"], x["ok"]) for x in v["snapshots"]] == [(7, True), (8, True)]


def test_replica_scrubs_kv_secrets_whatever_sqlites_secure_delete_default(env, tmp_path, monkeypatch):
    import types
    p, replica, ident_dir = _replica_project(env, tmp_path)
    token = "tok-" + "s3cr3t" * 40
    p.db.set_kv("slack_bot_token", token)

    class Insecure(sqlite3.Connection):   # a build whose default leaves deleted bytes in free pages
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.execute("PRAGMA secure_delete=OFF")

    monkeypatch.setattr(replica, "sqlite3", types.SimpleNamespace(
        connect=lambda *a, **k: sqlite3.connect(*a, factory=Insecure, **k)))
    assert replica.snapshot(p)["outcome"] == "ok"
    raw = gzip.decompress((ident_dir / "000001" / "project.db.gz").read_bytes())
    assert b"s3cr3ts3cr3t" not in raw


def test_replica_an_idle_project_with_a_current_standby_is_not_stale(env, tmp_path, monkeypatch):
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    p, replica, ident_dir = _replica_project(env, tmp_path)
    t0 = time.time()
    assert replica.snapshot(p, now=t0)["outcome"] == "ok"
    s = replica.settings(p.config())
    later = t0 + s["stale_s"] + 600
    assert replica.stale_text(p, p.config(), later), "with no check since, it is stale"
    assert replica.snapshot(p, now=later)["outcome"] == "unchanged"
    assert replica.stale_text(p, p.config(), later + 60) == ""
    line = replica.status_line(p, p.config(), later + 60)
    assert "snapshot #1" in line and "STALE" not in line and "nothing new since" in line
    assert f"#1 {replica._ago(later + 60 - t0)} old" in line, "status still shows the snapshot's own age"
    d = Daemon(p.base)
    d.cfg = p.config()
    seen = []
    monkeypatch.setattr(d, "observe", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(replica, "snapshot", lambda *a, **k: {"outcome": "unchanged"})
    monkeypatch.setattr(time, "time", lambda: later + 60)
    d.replicate_state()
    if d._replica_job:
        d._replica_job.join(10)
    assert not seen, "no replica-stale observation while the standby holds the current state"
    assert "STALE" not in status_text(p)


def test_replica_a_crash_mid_write_never_leaves_a_visible_snapshot(env, tmp_path):
    p, replica, ident_dir = _replica_project(env, tmp_path)
    cfg, s = p.config(), replica.settings(p.config())
    ident = replica.identity(p, cfg)
    stage = tmp_path / "stage"
    man = replica.build(p, cfg, stage, {}, 1, time.time())
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as t:
        for rel in [*man["files"], "manifest.json"]:
            t.add(str(stage / rel), arcname=rel)
    data = buf.getvalue()
    cut = data.rindex(b"manifest.json") + 512 + 40   # inside the manifest, the last member
    cmd = replica._receiver_cmd(cfg, s["dest"], "receive", ident, 1, 3, 1)
    # the sender dies half way: the stream just ends
    r = subprocess.run(cmd, input=data[:cut], capture_output=True)
    assert r.returncode == 5 and not (ident_dir / "000001").exists()
    assert not list(ident_dir.glob("*.partial")), "a failed receive cleans up after itself"
    # the receiver itself is killed mid-write: only a .partial is left, which verify does not count
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(data[:cut])
    proc.stdin.flush()
    assert _wait_for(lambda: list(ident_dir.glob("*.partial")))
    proc.kill()
    proc.communicate()
    assert list(ident_dir.glob("*.partial")) and not (ident_dir / "000001").exists()
    assert replica.verify(cfg, ident)["snapshots"] == []
    with pytest.raises(replica.SnapshotError, match="no complete snapshot"):
        replica.restore(ident_dir, tmp_path / "standin")
    # a manifest that does not match what arrived is refused too
    r = subprocess.run(cmd, input=data.replace(man["files"]["project.db.gz"]["sha256"].encode(), man["files"]["project.db.gz"]["sha256"].encode()[::-1], 1), capture_output=True)
    assert r.returncode == 5 and not (ident_dir / "000001").exists()
    # the next whole one lands, and the leftover .partial is gone
    r = subprocess.run(cmd, input=data, capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (ident_dir / "000001").is_dir() and not list(ident_dir.glob("*.partial"))


def test_replica_verify_finds_damaged_and_broken_chains(env, tmp_path):
    p, replica, ident_dir = _replica_project(env, tmp_path)
    for i in range(3):
        _replica_harness_commit(p, f"v{i}.md", str(i))
        assert replica.snapshot(p)["outcome"] == "ok"
    gz = ident_dir / "000003" / "project.db.gz"
    gz.write_bytes(gz.read_bytes()[:-5] + b"xxxxx")
    shutil.rmtree(ident_dir / "000001")
    v = {x["seq"]: x for x in replica.verify(p.config(), replica.identity(p, p.config()))["snapshots"]}
    assert "chain" in v[2]["why"] and "checksum" in v[3]["why"] and not v[2]["ok"]
    with pytest.raises(replica.SnapshotError, match="no complete snapshot"):
        replica.restore(ident_dir, tmp_path / "standin")
    # a broken chain makes the next snapshot a full one
    _replica_harness_commit(p, "w.md", "w")
    r = replica.snapshot(p)
    assert r["outcome"] == "failed" and "chain" in r["error"]
    r = replica.snapshot(p)
    assert r["outcome"] == "ok" and r["full"], r


def test_replica_ships_over_ssh_to_the_standby_home(env, tmp_path, monkeypatch):
    from ttp import replica
    p = make(env)
    bindir, remote_home = tmp_path / "bin", tmp_path / "remote-home"
    bindir.mkdir()
    remote_home.mkdir()
    # a stand-in ssh: drops its options and the host, runs the command with the standby's HOME
    (bindir / "ssh").write_text("#!/bin/sh\nwhile [ \"$1\" = -o ]; do shift 2; done\nshift\n"
                                f"HOME={shlex.quote(str(remote_home))} exec sh -c \"$*\"\n")
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    p.set_config("failover.standby", "standby-box")
    r = replica.snapshot(p)
    assert r["outcome"] == "ok", r
    ident = replica.identity(p, p.config())
    assert (remote_home / ".tt-project" / "standby" / ident / "000001" / "manifest.json").is_file()
    v = replica.verify(p.config(), ident)
    assert v["snapshots"] == [{"seq": 1, "ok": True, "why": ""}]


def test_replica_status_line_and_one_stale_observation(env, tmp_path, monkeypatch):
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp import screen as scr
    p, replica, ident_dir = _replica_project(env, tmp_path)
    assert "no snapshot yet" in status_text(p)
    assert replica.snapshot(p)["outcome"] == "ok"
    assert "snapshot #1" in status_text(p) and "STALE" not in status_text(p)
    st = replica.load_state(p)
    st["last"]["at"] = st["last"]["current_at"] = st["at"] = time.time() - 3 * 3600
    replica._save_state(p, st)
    assert "STALE" in status_text(p)
    d = Daemon(p.base)
    d.cfg = p.config()
    seen, taken = [], []
    monkeypatch.setattr(d, "observe", lambda *a, **k: seen.append((a, k)))
    monkeypatch.setattr(replica, "snapshot", lambda *a, **k: taken.append(1) or {"outcome": "failed", "error": "unreachable"})
    for _ in range(3):
        d._replica_checked = 0.0
        d.replicate_state()
        if d._replica_job:
            d._replica_job.join(10)
    assert taken, "an old snapshot makes the next one due at once"
    assert len(seen) == 1 and seen[0][1]["key"] == replica.OBS_KEY and "standby" in seen[0][0][1]
    closed = []
    monkeypatch.setattr(scr, "close_watcher_issues", lambda db, source, **k: closed.append((source, k.get('why'))) or 0)
    st = replica.load_state(p)
    st["last"]["at"] = st["last"]["current_at"] = st["at"] = time.time()
    replica._save_state(p, st)
    d._replica_checked = 0.0
    d.replicate_state()
    assert closed == [(replica.OBS_SOURCE, "the standby holds the current state")] and len(seen) == 1, "a current standby closes it, raising nothing new"
