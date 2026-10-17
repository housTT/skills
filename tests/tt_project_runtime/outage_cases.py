# Machine-ledger area: outage hand-offs as tracked tasks (outagetask.py). A long outage whose recovery owner
# cannot act, or records no change-ledger entry within heal.outage_owner_budget_min, becomes a task with a due
# time in the fallback (else a detecting project); overdue, one high page to its project, then one normal
# user alert. Exec'd into test_tt_project_runtime.py: make, _inbox and its imports are in scope.


def _outage_box(env, home="beta", **boxes):
    """Project `home` (the routing daemon's) and each box alias -> (owner, fallback): (p, ml)."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    p = make(env, home)
    for alias, (owner, fallback) in (boxes or {"box-a": ("owner", "beta")}).items():
        mm.add(alias, tags="device")
        mm.set_recovery(alias, owner=owner, fallback=fallback)
    return p, ml


def _no_quiet(c, now, doc):
    return "", False


def _route(ml, at, able, **kw):
    """Route once at `at`, every page delivered: [(alias, kind, target, severity)]."""
    out = []
    for x in ml.route(at, able=lambda n: able.get(n, (False, "not a project of this user")),
                      healthy=lambda *a: [], quiet=_no_quiet, **kw):
        ml.delivered(x)
        out.append((x["alias"], x["kind"], x["target"], x.get("severity")))
    return out


def _tasks(pages):
    return [(a, t) for a, k, t, _ in pages if k == "machine_outage_task"]


def test_an_owner_that_cannot_act_gets_an_outage_task_in_the_fallback_else_the_detector(env):
    """(a) At the first routing past the grace: the task goes to the fallback when it can act, else to the
    detecting project (the co-tenant that reported the condition)."""
    p, ml = _outage_box(env, **{"box-a": ("owner", "beta"), "box-b": ("owner", "delta")})
    t0 = time.time() - 3600
    for alias in ("box-a", "box-b"):
        ml.report("gamma", alias, "down", "no answer to ssh", now=t0)
    able = {"owner": (False, "it is paused"), "beta": (True, ""), "gamma": (True, ""), "delta": (False, "stopped")}
    pages = _route(ml, t0 + ml.GRACE_S, able)
    assert sorted(_tasks(pages)) == [("box-a", "beta"), ("box-b", "gamma")], pages
    assert _tasks(_route(ml, t0 + ml.GRACE_S + 600, able)) == [], "queued once per episode"


def test_an_owner_that_records_no_change_in_its_budget_gets_an_outage_task(env):
    """(b) The owner can act: no task until heal.outage_owner_budget_min (60) from the outage's start; then
    one, unless a change-ledger entry on the machine was recorded since the outage began."""
    from ttp import outagetask
    p, ml = _outage_box(env, **{"box-a": ("owner", "beta"), "box-b": ("owner", "beta")})
    assert outagetask.budgets(None)["owner_s"] == 3600 == outagetask.budgets({"heal": {}})["owner_s"]
    t0 = time.time() - 4 * 3600
    ml.add_change("box-a", "an older drop-in", "rm it", "owner", expires="2d", now=t0 - 600)
    able = {"owner": (True, ""), "beta": (True, "")}
    for alias in ("box-a", "box-b"):
        ml.report("gamma", alias, "held", now=t0)
    got = []
    for m in range(30, 91, 5):
        at = t0 + m * 60
        for alias in ("box-a", "box-b"):
            ml.report("gamma", alias, "held", now=at)
        if m == 40:
            ml.add_change("box-b", "power-cycled the board", "none", "owner", expires="2d", now=at)
        got += [(m, a, t) for a, t in _tasks(_route(ml, at, able))]
    assert got == [(60, "box-a", "beta")], "an older change counts for no later outage; box-b's owner acted"
    # A shorter budget from the routing project's config.
    from ttp import machines as mm
    mm.add("box-c", tags="device")
    mm.set_recovery("box-c", owner="owner", fallback="beta")
    ml.report("gamma", "box-c", "held", now=t0)
    short = outagetask.budgets({"heal": {"outage_owner_budget_min": 40}})
    assert short["owner_s"] == 2400
    assert _tasks(_route(ml, t0 + 39 * 60, able, budgets=short)) == []
    assert _tasks(_route(ml, t0 + 40 * 60, able, budgets=short)) == [("box-c", "beta")]


def test_outage_budget_settings_are_validated_and_a_bad_one_keeps_the_default(env):
    """(3) heal.outage_*_min: generic defaults 60/30/30; a value outside 1 minute to a week (or not a number)
    is a config problem, refused by `ttp config`, and budgets() keeps the default for it."""
    from ttp import cli, outagetask
    from ttp.project import config_problems, unknown_key_hint
    p = make(env, "beta")
    assert outagetask.budgets(None) == {"owner_s": 3600.0, "due_s": 1800.0, "escalate_s": 1800.0}
    for name, _ in outagetask.KEYS.values():
        assert unknown_key_hint(f"heal.{name}") is None, name
    cli.main(["config", p.name, "heal.outage_owner_budget_min", "45"])
    assert p.config()["heal"]["outage_owner_budget_min"] == 45
    assert config_problems(p.raw_config()) == []
    assert outagetask.budgets(p.config())["owner_s"] == 45 * 60
    for bad in ("0", "soon", "true", str(outagetask.MAX_MIN + 1)):
        with pytest.raises(SystemExit):
            cli.main(["config", p.name, "heal.outage_task_due_min", bad])
    assert "outage_task_due_min" not in p.config()["heal"], "a refused value is not stored"
    raw = {"heal": {"outage_escalate_min": 0, "outage_task_due_min": "30"}}
    assert len(config_problems(raw)) == 2 and all("heal.outage_" in x for x in config_problems(raw))
    assert outagetask.budgets(raw) == outagetask.budgets(None), "an invalid value keeps the default"


def test_an_overdue_outage_task_pages_its_project_high_then_the_user_normal_each_once(env):
    """(c) One high page to the task's project at its due time, one normal user alert heal.outage_escalate_min
    later; neither repeats."""
    p, ml = _outage_box(env)
    t0 = time.time() - 6 * 3600
    able = {"owner": (False, "it is paused"), "beta": (True, "")}
    late = []
    for m in range(0, 241, 10):
        at = t0 + m * 60
        ml.report("gamma", "box-a", "held", now=at)
        late += [(m, t, s) for a, k, t, s in _route(ml, at, able, task_status=lambda proj, lab: "queued")
                 if k == "machine_outage_overdue"]
    assert late == [(60, "beta", "high"), (90, ml.USER, "normal")], late


@pytest.mark.parametrize("status,escalates,listed", [("cancelled", False, False), ("done", False, False),
                                                     ("failed", True, True), ("queued", True, True),
                                                     (None, True, None)])
def test_one_closed_state_rule_for_outage_escalation_and_the_open_lists(env, status, escalates, listed):
    """Done and cancelled outage tasks are closed: no overdue page, not listed open. A failed one stays open:
    listed in status, web and sweep, and it escalates. An unreadable state (None) escalates."""
    from ttp import outagetask, sweep, web
    p, ml = _outage_box(env)
    t0 = time.time() - 7200
    ml.report("gamma", "box-a", "down", now=t0)
    able = {"owner": (False, "it is paused"), "beta": (True, "")}
    kw = dict(task_status=lambda proj, lab: status)
    assert _tasks(_route(ml, t0 + ml.GRACE_S, able, **kw)) == [("box-a", "beta")]
    late = [k for a, k, t, s in _route(ml, t0 + ml.GRACE_S + 1860, able, **kw)]
    assert ("machine_outage_overdue" in late) is escalates, late
    assert outagetask.closed(status) is (not escalates)
    if listed is None:
        return
    now = time.time()
    rec = {"alias": "box-a", "condition": "down", "first": t0, "due": t0 + 3600, "why": "w", "tried": "t",
           "seen_by": ["gamma"], "label": f"outage:box-a:down@{int(t0)}"}
    tid = outagetask.ensure(p.db, rec, p, now)
    p.db.update_task(tid, status=status)
    assert ([t["id"] for t in outagetask.open_tasks(p.db)] == [tid]) is listed
    assert any(f"outage #{tid}" in x for x in web.health(p, p.db, now=now)["outages"]) is listed
    assert any(i["key"] == f"task:{tid}" and i["kind"] == "outage" for i in sweep.items(p.db, now)) is listed


def test_every_outage_hand_off_path_creates_a_task_row_never_only_a_note(env):
    """(d) The routing daemon's own project: Daemon._machine_page creates the task row (outagetask.ensure),
    once, with no note. Another project: a daemon note carrying the outage record, and the target's daemon
    files the task on reading it (upstream._file). A worker's note with such a record files none."""
    from ttp import upstream
    from ttp.daemon import Daemon
    p, ml = _outage_box(env)
    t0 = time.time() - 3600
    ml.report("gamma", "box-a", "down", "no answer", now=t0)
    page = next(x for x in ml.route(t0 + ml.GRACE_S, able=lambda n: (n == "beta", "it is paused"),
                                    healthy=lambda *a: [], quiet=_no_quiet) if x["kind"] == "machine_outage_task")
    assert page["target"] == "beta" and page["outage"]["label"] == f"outage:box-a:down@{int(t0)}"
    d = Daemon(p.base)
    assert d._machine_page(page, t0 + ml.GRACE_S) and d._machine_page(page, t0 + ml.GRACE_S)
    rows = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%outage:%'")
    assert len(rows) == 1 and rows[0]["status"] == "queued", rows
    assert json.loads(rows[0]["labels"]) == [page["outage"]["label"], f"due:{int(page['outage']['due'])}"]
    assert "owner owner cannot act: it is paused" in rows[0]["spec"]
    assert _inbox() == [], "at home: the task, no note"
    # Another project: the note carries the record; its daemon files the task when it reads the note.
    assert d._machine_page({**page, "target": "delta"}, t0 + ml.GRACE_S)
    notes = _inbox()
    assert [(n["to"], n["from"]) for n in notes] == [("delta", "daemon")] and notes[0]["outage"] == page["outage"]
    p.db.x("DELETE FROM tasks")
    rec = {**page["outage"], "label": f"outage:box-a:down@{int(t0) + 1}"}
    for _ in range(2):
        assert upstream.send("gamma", None, "beta", "an outage task", "high", sender="daemon",
                             extra={"outage": rec}) in ("sent", "duplicate")
    assert upstream.send("gamma", 1, "beta", "a worker", "normal", extra={"outage": {**rec, "label": "outage:x:y@1"}})
    upstream.ingest(p, p.config())
    upstream.ingest(p, p.config())
    rows = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%outage:%'")
    assert [json.loads(r["labels"])[0] for r in rows] == [rec["label"]], "one task; a worker's note files none"


def test_outage_replay_held_box_paused_owner_muted_watcher_reaches_a_task_in_time(env, monkeypatch):
    """(f) Incident replay: a box held for 4 h, its owner paused, the watcher that sees it muted. The routing
    project's daemon files the task within budgets()['due_s'] of the grace, the overdue page at its due time
    goes to its coordinator at high effort, and the user's alert follows once."""
    from ttp import coordinator as coord
    from ttp import daemon as dm
    from ttp import outagetask
    from ttp import screen as scr
    p, ml = _outage_box(env)
    b = outagetask.budgets(p.config())
    clock = {"t": time.time() - 5 * 3600}
    t0 = clock["t"]
    monkeypatch.setattr(scr.time, "time", lambda: clock["t"])
    held = json.dumps({"text": "box-a: held by the broker", "severity": "high", "repeat": True,
                       "machine": "box-a", "condition": "held"})
    monkeypatch.setattr(dm.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, held, ""))
    d = dm.Daemon(p.base)
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert coord.apply(p, [{"type": "observation_mute", "source": "watcher:hw", "match": "held",
                            "hours": 24, "why": "known"}]) == []
    seen = []
    for m in range(0, 241, 10):
        clock["t"] = t0 + m * 60
        d._run_command_watcher({"name": "hw"}, {"command": "x"})
        for x in ml.route(clock["t"], able=lambda n: (n != "owner", "it is paused"), healthy=lambda *a: [],
                          quiet=_no_quiet, budgets=b):
            assert d._machine_page(x, clock["t"])
            ml.delivered(x)
            seen.append((m, x["target"], x["kind"]))
    assert scr.mutes(p.db, clock["t"]), "the watcher stayed muted throughout"
    got = [s for s in seen if s[2] != "machine_condition"]
    assert got == [(30, "beta", "machine_outage_task"), (60, "beta", "machine_outage_overdue"),
                   (90, ml.USER, "machine_outage_overdue")], seen
    assert got[0][0] * 60 <= ml.GRACE_S + b["due_s"]
    t = p.db.one("SELECT * FROM tasks WHERE labels LIKE '%outage:box-a:held%'")
    assert t and t["status"] == "queued" and t["created"] - t0 <= ml.GRACE_S + b["due_s"]
    ev = p.db.one("SELECT * FROM events WHERE kind='machine_outage_overdue'")
    assert ev["severity"] == "high" and coord.EFFORT_EVENT_TRIGGERS["machine_outage_overdue"] == "resource"
    alerts = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'outage:box-a:held@%#user'")
    assert [a["severity"] for a in alerts] == ["normal"]


def test_open_outage_tasks_show_in_status_web_and_the_sweep(env):
    """(g) Smoke: the outage line in `ttp status` and the web health, and the sweep's outage item: listed
    'OUTAGE TASK, NOT DUE YET' before its due time, OVERDUE after; one item when the task is also stuck."""
    from ttp import cli, outagetask, sweep, web
    p = make(env)
    now = time.time()
    rec = {"alias": "box-a", "condition": "held", "first": now - 5400, "due": now - 1200, "why": "w",
           "tried": "t", "seen_by": ["gamma"], "label": f"outage:box-a:held@{int(now - 5400)}"}
    tid = outagetask.ensure(p.db, rec, p, now)
    assert f"outage #{tid} box-a held for 1.5 h, task queued" in cli.status_text(p)
    assert any(f"outage #{tid} box-a held" in x and "OVERDUE" in x for x in web.health(p, p.db, now=now)["outages"])
    late = next(i for i in sweep.items(p.db, now) if i["key"] == f"task:{tid}")
    assert late["kind"] == "outage" and "OVERDUE" in late["reason"] and late["ends"] is None
    early = next(i for i in sweep.items(p.db, now - 1500) if i["key"] == f"task:{tid}")
    assert "OVERDUE" not in early["reason"] and early["ends"] == "due"
    assert "OUTAGE TASK, NOT DUE YET" in sweep._line(early, {}, {}, now - 1500)
    p.db.update_task(tid, status="blocked")
    stuck = [i for i in sweep.items(p.db, now) if i["task"] == tid]
    assert len(stuck) == 1 and stuck[0]["kind"] == "outage" and "OVERDUE" in stuck[0]["reason"], stuck
