# Alerts tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new alerts tests here, not there.
from __future__ import annotations


def test_unreadable_config_starts_no_model_work_and_alerts_once(env, monkeypatch):
    """Neither project.json nor a last good copy: no coordinator turn or worker run starts on the
    defaults; one alert says so and clears once the file reads again."""
    from ttp import alerts
    from ttp.daemon import Daemon
    p = make(env)
    saved = p.config_path.read_text()
    p.config_path.write_text("{ broken")
    (p.state / "project.last-good.json").unlink(missing_ok=True)
    p.db.add_task("user work", "s", kind="work", tier="light", origin="user")
    p.db.post("in", "hello", chat="web")
    d = Daemon(p.base)
    assert d.cfg_status == "unavailable"
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(a[0]) or 0)
    for _ in range(2):
        d._last_cfg = 0
        d.tick()
    assert started == [], "no turn or run routes on the defaults"
    eps = p.db.q("SELECT * FROM alerts WHERE key='config' AND cleared IS NULL")
    assert len(eps) == 1 and p.db.one("SELECT COUNT(*) n FROM messages WHERE ref='config'")["n"] == 1
    assert alerts.active(p.db, "config", eps[0]["raised"], time.time())
    with pytest.raises(RuntimeError):
        Daemon.start_run(d, "worker", "x", "fake", "light", str(p.root))
    p.config_path.write_text(saved)
    d._load_config()
    d.sweep_alerts()
    assert d.cfg_status == "ok"
    assert p.db.one("SELECT cleared FROM alerts WHERE key='config'")["cleared"]
    assert p.db.one("SELECT id FROM messages WHERE ref='config' AND kind='resolved'")



def test_a_machine_wide_alert_is_broadcast_by_one_project_and_shown_quietly_by_the_others(env, monkeypatch):
    p = make(env)
    from ttp import alerts, notifier
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)   # no daemon runs in the test
    from ttp.cli import bootstrap
    from ttp.daemon import Daemon
    from ttp.web import attention
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    for x in (p, q):
        x.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    Daemon(q.base).alert("auth:fake", "fake is logged out", "high")
    assert [m["text"] for m in p.db.unread_for_chat("c1", 0)] == ["fake is logged out"]
    assert q.db.unread_for_chat("c1", 0) == [], "the second project told the chat again"
    assert [a["cleared"] for a in notifier.alerts_since(q, 0)] == [True], "the desktop would show it twice"
    assert [m["text"] for m in attention(q.db, time.time())] == ["fake is logged out"], "web and status show it"
    # A condition only one project has (its own schedule) is not machine-wide.
    Daemon(q.base).alert("coordinator", "The coordinator failed", "high")
    q.db.set_kv("coordinator_failures", 1)
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["The coordinator failed"]
    # Once the claimer's episode clears, the claim is released: the other project may broadcast.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(p.base).sweep_alerts()
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(q.base).sweep_alerts()
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["The coordinator failed"], \
        "a quiet episode clears quietly"
    Daemon(q.base).alert("auth:fake", "fake is logged out again", "high")
    Daemon(p.base).alert("auth:fake", "fake is logged out again", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][-1] == "fake is logged out again"
    assert [m["text"] for m in p.db.unread_for_chat("c1", 0)][-1] == "Cleared: fake is logged in again; its queued work starts."



def _two_projects(env):
    from ttp.cli import bootstrap
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    for x in (p, q):
        x.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
    return p, q



def test_a_claim_from_an_earlier_logout_does_not_silence_the_next_one(env, monkeypatch):
    # p broadcast the first logout and stays idle (its episode and claim stay open); q posted it
    # quietly. The user logs in again and only q runs, so only q's episode clears.
    from ttp import alerts
    from ttp.daemon import Daemon
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)
    p, q = _two_projects(env)
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    Daemon(q.base).alert("auth:fake", "fake is logged out", "high")
    assert q.db.unread_for_chat("c1", 0) == []
    stale = json.loads(alerts.claims_path().read_text())
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))
    Daemon(q.base).sweep_alerts()
    assert _episodes(p, "auth:fake")[0]["cleared"] is None and _episodes(q, "auth:fake")[0]["cleared"]
    assert json.loads(alerts.claims_path().read_text()) == {}, "the clear drops the idle project's claim"
    Daemon(q.base).alert("auth:fake", "fake is logged out again", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)] == ["fake is logged out again"], \
        "a new logout reached no chat"
    # Even a claim the clear could not drop (an older runtime, a failed write) is stale for a
    # project whose own episode cleared after it was taken.
    q.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 2, time.time() + 2))
    Daemon(q.base).sweep_alerts()
    alerts.claims_path().write_text(json.dumps(stale))
    Daemon(q.base).alert("auth:fake", "fake is logged out a third time", "high")
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][-1] == "fake is logged out a third time"



def test_a_stopped_claimers_claim_does_not_silence_the_others(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p, q = _two_projects(env)
    running = {str(p.base), str(q.base)}
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: owner in running)
    start = time.time()
    clock = {"t": start}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    dq = Daemon(q.base)
    dq.open_breaker("fake", "logged out")
    dq.alert("auth:fake", "fake is logged out", "high")
    assert q.db.unread_for_chat("c1", 0) == []
    running.discard(str(p.base))   # p's daemon stops for good, still holding the claim
    clock["t"] = start + alerts.REMIND_S + 1800   # before the claim's TTL runs out
    assert clock["t"] - start < alerts.CLAIM_TTL
    dq.remind_logouts()
    assert [m["text"] for m in q.db.unread_for_chat("c1", 0)][0].startswith("fake on testhost is still logged out"), \
        "the reminder was left to a stopped project"
    from ttp.project import hostname
    assert json.loads(alerts.claims_path().read_text())[f"{hostname()}|auth:fake"]["owner"] == str(q.base)



def test_a_logout_is_one_alert_per_machine_reminded_at_1_4_and_12_hours_with_what_waits(env, monkeypatch):
    from ttp import alerts
    from ttp.daemon import Daemon
    p, q = _two_projects(env)
    running = {str(p.base), str(q.base)}
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: owner in running)
    start = time.time()
    clock = {"t": start}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    dp, dq = Daemon(p.base), Daemon(q.base)
    for d in (dp, dq):
        d.open_breaker("fake", "logged out")
        d.alert("auth:fake", "fake is logged out", "high")
    q.db.add_task("queued work", "")
    q.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (start, "watch", "obs", "high", "a box is down", "queued"))
    told = lambda x: [m["text"] for m in x.db.unread_for_chat("c1", 0)]
    seen = []
    for hours in (0.5, 1.01, 2, 3.9, 4.01, 8, 11.9, 12.01, 20, 35.9, 36.01):
        clock["t"] = start + hours * 3600
        for d in (dq, dp):   # each project reports every few minutes; here the claimer goes last
            d._logout_round = 0.0
            d.remind_logouts()
        seen.append((hours, len(told(p))))
    assert told(q) == [], "the second project paged the chat too"
    sent = told(p)
    assert sent[0] == "fake is logged out" and len(sent) == 5, sent
    assert [h for (h, n), (_, m) in zip(seen[1:], seen) if n > m] == [1.01, 4.01, 12.01, 36.01]
    assert all("still logged out" in t and "2 project(s) wait on it (demo, second)" in t
               and "1 queued task(s) and 1 high event(s)" in t for t in sent[1:]), sent[1]
    rows = p.db.q("SELECT channel, severity FROM messages WHERE ref='auth:fake'")
    assert {(r["channel"], r["severity"]) for r in rows} == {("chat", "high")}, "a reminder went out quietly"
    assert len(_episodes(p, "auth:fake")) == 1, "a reminder opened a second episode"
    # The claimer stops: the other project keeps the cadence without repeating a reminder.
    running.discard(str(p.base))
    clock["t"] = start + 36.5 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert told(q) == []
    clock["t"] = start + 60.01 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert len(told(q)) == 1 and "1 project(s) wait on it (second)" in told(q)[0]
    # Logged in again: no more reminders.
    dq._close_breaker("fake", alerts.breaker(q.db, "fake"), "login check passed")
    clock["t"] = start + 90 * 3600
    dq._logout_round = 0.0
    dq.remind_logouts()
    assert len(told(q)) == 1



def test_account_mismatch_then_match_keeps_caps_meanwhile_and_alerts_once(env, monkeypatch):
    """A login on another account than the plan readings, its kind unknown: the dollar caps hold it
    (the stricter side), one self-clearing status line says so (never 'needs you'), and once a run of
    the logged-in account reports its windows, the plan windows gate it again with no manual step and
    the line clears itself."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    now = time.time()
    _old_plan(p, now)
    from ttp.providers.claude import Claude
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | plan")
    bud.note_account(p.db, "claude", "acct-b | plan")     # the login switched
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.numbers.get("account_mismatch") and any(
        "no plan readings of its own yet" in r for r in g.reasons), g.reasons
    assert not any("check the login" in r or "another account" in r for r in g.reasons), g.reasons
    p.set_config("core_provider", "claude")
    d = Daemon(p.base)
    d.update_gates()
    d.update_gates()                                      # a repeat does not post again
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low", [dict(m) for m in sent]
    assert "log in" not in sent[0]["text"] and "clears by itself" in sent[0]["text"]
    assert alerts.holds(p.db, "account-mismatch:claude", now, now)
    assert not alerts.needs_you(p.db, time.time()), "a status line, never 'needs you'"
    assert any(m["state"] == "status" and "no plan readings" in m["text"] for m in alerts.feed(p.db, time.time()))
    # the next run on the logged-in account reports its windows
    _run(p, "acct-b | plan", now - 120, now - 30, 0.5)
    _reading(p, time.time(), "acct-b | plan", "five_hour", 7.0, now + 3600)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert g.regime == "windows" and not g.numbers.get("account_mismatch"), g.reasons
    d.update_gates()
    assert not alerts.holds(p.db, "account-mismatch:claude", now, time.time())
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='account-mismatch:claude'")["cleared"]



def _billed_switch(p, account="acct-b | contracted"):
    """The login moved off the plan account to `account`, and the budget fingerprint recorded the
    plan -> billed change for it (budgetkind.apply's event; it reflects the gate, so it is no evidence)."""
    from ttp import budget as bud, budgetkind
    bud.note_account(p.db, "claude", account)
    ts = time.time() - 300
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
           (ts, "daemon", budgetkind.KIND, "normal", "claude budget changed", "handled",
            json.dumps({"provider": "claude", "old": {"account": budgetkind.account_hash("acct-a | plan"), "kind": "plan"},
                        "new": {"account": budgetkind.account_hash(account), "kind": "billed"}})))
    return ts



def test_switch_to_usage_billed_account_is_a_neutral_line_not_needs_you(env, monkeypatch):
    """The reported case: plan readings of account A, claude deliberately switched to a usage-billed
    account B, whose paid runs since report no plan windows (the lapse rule). The old readings are history: no 'another
    account' reason and no alert or 'needs you', a neutral 'billed by use since' line in the gate's
    reasons (status, web app, coordinator digest), and the dollar caps still gate it. An alert raised
    for the condition before clears itself."""
    p = make(env)
    from ttp import alerts, budget as bud, timefmt
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    _old_plan(p, now)
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | contracted")
    # an alert raised for the condition by an earlier version (normal, 'needs you' then)
    p.db.post("out", "claude is logged in to another account than its plan readings", chat=None, kind="alert",
              severity="normal", ref="account-mismatch:claude")
    bud.note_account(p.db, "claude", "acct-b | contracted")
    since = now - 500
    _run(p, "acct-b | contracted", since, now - 400, 0.3)
    _run(p, "acct-b | contracted", now - 300, now - 200, 0.3)
    p.set_config("budget.daily_usd", 5)
    p.db.spend("claude", 9.0, "task:2", account="acct-b | contracted", ts=now - 60)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and not g.numbers.get("account_mismatch"), g.reasons
    assert not any("another account" in r or "plan readings" in r or "login" in r for r in g.reasons), g.reasons
    line = f"claude billed by use since {timefmt.short(since, p.config(), now)}"
    assert line in g.reasons and g.numbers["billed_by_use_since"] == since, g.reasons
    assert g.level == "red" and any("cap reached" in r for r in g.reasons), g.reasons   # the dollar cap still applies
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    assert len(p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")) == 1, "no new alert"
    assert not alerts.holds(p.db, "account-mismatch:claude", now, time.time())
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='account-mismatch:claude'")["cleared"]
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "account" in (m.get("ref") or "")]
    assert line in p.db.kv("gates")["claude"]["reasons"]



@pytest.mark.parametrize("stale", [False, True])
def test_usage_billed_account_on_day_one_is_never_told_to_log_in(env, monkeypatch, stale):
    """A usage-billed account before its first run: no plan readings at all (a new project), or only an
    old plan account's (stale). Neither is a login problem: no gate reason, alert, feed line or
    'needs you' item says log in or names the login."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    if stale:
        _old_plan(p, now)
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | contracted")
    bud.note_account(p.db, "claude", "acct-b | contracted")
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    said = list(p.db.kv("gates")["claude"]["reasons"]) + [m["text"] for m in p.db.q("SELECT text FROM messages")]
    said += [m["text"] for m in alerts.feed(p.db, time.time())]
    assert not [t for t in said if re.search(r"\blog(?:ged)?[ -]?(?:in|out)\b|\blogin\b", t, re.I)], said
    assert not alerts.needs_you(p.db, time.time())



@pytest.mark.parametrize("text", [
    "claude is logged in to another account than its plan readings. Please log in again to the plan account.",
    "Plan readings for claude are missing since the switch; please check which account claude is logged in as.",
    "claude may be logged out: no plan windows reported for 2 days. Run claude there and use /login.",
])
def test_a_login_ask_or_notify_needs_a_real_auth_failure(env, text):
    """The coordinator cannot tell the user to log an agent CLI in again from plan metering alone:
    with no open auth breaker or auth alert the ask and the notify are refused. A real auth failure
    (a run refused at the login opens the breaker) lets the same ask through, and a login ask about
    something that is not an agent CLI is not this gate's."""
    from ttp import alerts, coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems and "no agent CLI here is logged out" in problems[0] and ask is None, problems
    assert p.db.one("SELECT 1 FROM events WHERE kind=? AND text LIKE 'false login:%'", (coord.ASK_REFUSED_KIND,))
    problems = coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}])
    assert problems and "notify rejected" in problems[0], problems
    assert not p.db.one("SELECT 1 FROM messages WHERE kind='alert' AND text=?", (text,))
    other = "The VPN portal session expired; please sign in again to the VPN."
    assert coord._false_login_ask(p.db, other) == ""
    Daemon(p.base).open_breaker("claude", "Failed to authenticate: OAuth session expired")
    assert alerts.breaker(p.db, "claude")
    assert coord._false_login_ask(p.db, text) == ""
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems == [] and ask is not None, problems



@pytest.mark.parametrize("text", [
    "gh on the device box is logged out, so Claude cannot push. Please run gh auth login again there.",
    "The Slurm account token expired; please re-authenticate with kinit so the claude workers can submit jobs.",
    "The build server's SSO session expired, so codex runs cannot fetch artifacts; please log in again there.",
    "Claude cannot reach the device box: the host is logged out after the reboot. Please log in again on the console.",
    "The fake data server is logged out; please log in again.",
    # review #2354: a provider before a clause break, after the phrase, or generic 'subscription'/'metering'
    "Claude needs the NAS share: please log in again on the file server.",
    "The license portal session expired, so claude cannot use the subscription seat; please log in again there.",
    "Please log in again to the dashboard so claude can read the device metering data.",
    "The license server logged out the claude runs; please log in again on the license portal.",
    "The claude build server is logged out; please log in again on it.",
    "Claude logged out of the NAS share; please log in again on the file server.",
    "The claude session on the jump host ended; please log in again on the jump host.",
    "Codex's login to the artifact store failed; please log in again there.",
])
def test_a_login_ask_about_another_credential_is_never_refused(env, text):
    """Reviews #2338 and #2354: the gate refuses only a login of the agent CLI itself (the provider is
    grammatically what is logged in) or one citing a plan-metering cue. An ask
    about another credential or tool (gh, Slurm, kinit, SSO, a host) that only names a provider in
    passing is a real ask, sent with no auth breaker open. The test-only fake provider is no name to
    match unless the project's config uses it."""
    from ttp import alerts, coordinator as coord
    p = make(env)
    p.set_config("core_provider", "claude")
    assert not any(alerts.breaker(p.db, n) for n in ("claude", "codex", "cursor", "fake"))
    assert coord._false_login_ask(p.db, text, p.config()) == ""
    problems, ask = _ask(p, text, blocking="access", recommendation="log in once")
    assert problems == [] and ask is not None, problems
    problems = coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}])
    assert problems == [], problems



def test_recorded_billed_change_from_the_mismatch_gate_is_no_evidence(env, monkeypatch):
    """Review #2298: budgetkind classifies the mismatch gate (dollar caps) as 'billed', so a switch
    to an account of unknown kind records a plan -> billed change after the debounce. That change
    reflects the gate itself: the warning stays a low status line until the account's own runs say
    which it is."""
    p = make(env)
    from ttp import alerts, budget as bud, budgetkind
    from ttp.daemon import Daemon
    from ttp.providers.claude import Claude
    now = time.time()
    _old_plan(p, now)
    p.set_config("core_provider", "claude")
    monkeypatch.setattr(Claude, "account", lambda self: "acct-a | plan")
    bud.note_account(p.db, "claude", "acct-a | plan")
    d = Daemon(p.base)
    d.update_gates()                                       # the baseline
    monkeypatch.setattr(Claude, "account", lambda self: "acct-b | plan")
    bud.note_account(p.db, "claude", "acct-b | plan")
    monkeypatch.setattr(budgetkind, "DEBOUNCE_S", 0.0)
    for _ in range(3):
        d.update_gates()
    ev = p.db.q("SELECT data FROM events WHERE kind=?", (budgetkind.KIND,))
    assert ev and json.loads(ev[-1]["data"])["new"]["kind"] == "billed", "the circular event is recorded"
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert g.numbers.get("account_mismatch") and "billed_by_use_since" not in g.numbers, g.reasons
    assert not any("billed by use since" in r for r in g.reasons), g.reasons
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low", [dict(m) for m in sent]
    assert not alerts.needs_you(p.db, time.time())
    # real evidence: two paid runs of its own that reported no plan windows
    _run(p, "acct-b | plan", now - 500, now - 400, 0.3)
    _run(p, "acct-b | plan", now - 300, now - 200, 0.3)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db), time.time())
    assert not g.numbers.get("account_mismatch") and g.numbers["billed_by_use_since"] == now - 500, g.reasons



def test_plan_account_missing_its_readings_still_warns_as_status_line(env):
    """A plan account (it reported plan windows before) whose own readings are missing still warns,
    as a self-clearing status line, even with a plan -> billed change recorded for it."""
    p = make(env)
    from ttp import alerts, budget as bud
    from ttp.daemon import Daemon
    now = time.time()
    _old_plan(p, now)
    _reading(p, now - 10 * 86400, "acct-b | plan", "seven_day", 40.0, now - 9 * 86400)
    _billed_switch(p, "acct-b | plan")
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.numbers.get("account_mismatch") and "billed_by_use_since" not in g.numbers, g.reasons
    p.set_config("core_provider", "claude")
    Daemon(p.base).update_gates()
    sent = p.db.q("SELECT * FROM messages WHERE ref='account-mismatch:claude'")
    assert len(sent) == 1 and sent[0]["severity"] == "low"
    assert alerts.tracked("alert", "low", "account-mismatch:claude") and not alerts._top("low", "account-mismatch:claude")
    assert not alerts.needs_you(p.db, time.time())



def test_a_broken_command_schedule_can_be_turned_off_without_a_command(env):
    p = make(env)
    from ttp import alerts
    from ttp import coordinator as coord
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    # A legacy command schedule that stored an llm-style payload and so has no command.
    sched.upsert(p.db, "probe", "command", "30m", payload={"spec": "python3 check.py", "tier": "light"})
    d = Daemon(p.base)
    d.gates = {}
    for _ in range(3):
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()
        if fix := p.db.one("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'"):
            p.db.update_task(fix["id"], status="failed")   # its fix task could not fix it: now the alert
    assert [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    # Enabling it without a command is still rejected.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out
    assert coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": False}]) == []
    s = p.db.one("SELECT * FROM schedules WHERE name='probe'")
    assert not s["enabled"] and s["every_s"] == 1800
    d.sweep_alerts()
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    # Switching it back on still needs the command.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out



def test_a_schedule_failing_twice_raises_one_alert_that_clears_on_an_ok_run(env):
    p = make(env)
    from ttp import alerts
    from ttp import schedule as sched
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    sched.upsert(p.db, "probe", "command", "30m", payload={})
    d = Daemon(p.base)
    d.gates = {}

    def tick():
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()

    def live():
        return [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]

    tick()
    assert not live()   # one failure may be a fluke
    assert "schedules failing: probe (no command)" in status_text(p)
    tick()
    # Two failures queue one harness task to fix it, with its errors; no alert yet.
    fixes = p.db.q("SELECT * FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")
    assert len(fixes) == 1 and fixes[0]["kind"] == "harness" and "no command" in fixes[0]["spec"]
    assert "harness/schedules.json" in fixes[0]["spec"]
    tick()
    assert not live() and len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")) == 1
    # Its fix task done while the schedule still fails: now the alert, naming the task.
    p.db.update_task(fixes[0]["id"], status="done")
    tick()
    tick()
    assert len(live()) == 1 and f"#{fixes[0]['id']} ended done" in live()[0]["text"]
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref='schedule:probe' AND kind='alert'")["n"] == 1
    p.db.x("UPDATE schedules SET payload=? WHERE name='probe'", (json.dumps({"command": "true"}),))
    tick()
    assert not live()
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    assert "schedules failing" not in status_text(p)
    # A new failure stint after the ok run queues a new fix task.
    p.db.x("UPDATE schedules SET payload=? WHERE name='probe'", (json.dumps({}),))
    tick()
    tick()
    assert len(p.db.q("SELECT id FROM tasks WHERE labels LIKE '%schedule_fix:probe%'")) == 2 and not live()



_HUNG_TICK = """import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from ttp.daemon import register_stack_dump
state = Path(sys.argv[1])
assert register_stack_dump(state)
(state / "stub.ready").write_text("1")


def tick():
    while True:
        pass


tick()
"""



def test_a_daemon_hung_inside_a_tick_is_dumped_restarted_alerted_once_and_cleared(env, monkeypatch, tmp_path):
    from ttp import alerts, service, watchdog
    p = make(env)
    stub = tmp_path / "stubs" / "hung_daemon.py"
    stub.parent.mkdir()
    stub.write_text(_HUNG_TICK)
    proc = subprocess.Popen([sys.executable, "-I", str(stub), str(p.state), str(RUNTIME)])
    live = {proc.pid}
    monkeypatch.setattr(watchdog, "_is_daemon", lambda pid: pid in live and _alive_child(pid, proc))
    monkeypatch.setattr(watchdog, "_alive", lambda pid: pid in live and _alive_child(pid, proc))
    monkeypatch.setattr(service, "installed", lambda p: {"kind": "systemd", "watchdog": True})
    calls = []

    def systemctl(*args, timeout):
        calls.append(args)
        if args[0] == "restart":   # systemd ends the hung daemon and starts a new one (this test process)
            proc.kill()
            proc.wait()
            (p.state / "daemon.pid").write_text(str(os.getpid()))
            live.add(os.getpid())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(watchdog, "_systemctl", systemctl)
    try:
        deadline = time.time() + 30
        while not (p.state / "stub.ready").exists() and time.time() < deadline:
            time.sleep(0.1)
        assert (p.state / "stub.ready").exists(), "the stub daemon never started"
        (p.state / "daemon.pid").write_text(str(proc.pid))
        (p.state / "daemon.start").write_text(json.dumps({"pid": proc.pid, "started": time.time() - 3600,
                                                          "stacks": True}))
        hb = p.state / "heartbeat"
        hb.write_text(json.dumps({"pid": proc.pid, "started": time.time() - 3600}))
        old = time.time() - watchdog.HANG_S - 100
        os.utime(hb, (old, old))
        now = time.time()
        assert watchdog.check(p, now=now) == "stale"
        assert proc.poll() is None and not calls
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S) == "restarted"
    finally:
        proc.kill()
        proc.wait()
    assert calls == [("restart", f"{service.unit_name(p)}.service")]
    rec = p.db.kv("daemon_hang")
    dump = pathlib.Path(rec["dump"])
    assert dump.parent == p.state / "incidents" and rec["restarted"] and rec["pid"] == proc.pid
    text = dump.read_text()
    assert "most recent call first" in text and "in tick" in text and "hung_daemon.py" in text, text
    alerts_sent = p.db.q("SELECT severity, text FROM messages WHERE ref='daemon-hang' AND kind='alert'")
    assert len(alerts_sent) == 1 and alerts_sent[0]["severity"] == "normal"
    assert "demo" in alerts_sent[0]["text"] and str(dump) in alerts_sent[0]["text"]
    assert "restarted it" in alerts_sent[0]["text"]
    assert alerts.holds(p.db, "daemon-hang", now, now)
    # The new daemon is in its first tick: the incident stays open, nothing is sent again.
    (p.state / "daemon.start").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
    assert watchdog.check(p) == "ok" and p.db.kv("daemon_hang")
    # It completes one: the incident ends and the alert clears itself.
    (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
    assert watchdog.check(p) == "ok" and not p.db.kv("daemon_hang")
    closed = alerts.sweep(p.db)
    assert [ep["key"] for ep in closed] == ["daemon-hang"]
    assert len(p.db.q("SELECT id FROM messages WHERE ref='daemon-hang' AND kind='alert'")) == 1



def _alive_child(pid, proc):
    return pid != proc.pid or proc.poll() is None



def test_an_auth_failure_opens_a_breaker_that_survives_a_restart_and_holds_only_its_provider(env, monkeypatch):
    err = "Error: Authentication required. Please run 'agent login' first, or set CURSOR_API_KEY.\n"
    p, run, task, _, _ = _cli_run(env, monkeypatch, "cursor", "", stderr=err, rc=1)
    from ttp import alerts, cli
    from ttp import budget as bud
    from ttp.daemon import LOGGED_OUT_NOTE, Daemon
    from ttp.providers import get_provider
    assert run["status"] == "auth" and alerts.breaker(p.db, "cursor"), "the first auth failure opens the breaker"
    p.db.update_task(task["id"], provider="cursor")
    other = p.db.add_task("other", "s", kind="work", tier="light", origin="user")   # the core provider
    d = Daemon(p.base)   # a restart: the breaker is read back from the project's state
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, *a, **k: started.append(k["task"]["id"]) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    for prov in ("fake", "cursor"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    monkeypatch.setattr(type(get_provider("cursor")), "login_check", lambda self: False)
    for _ in range(3):
        d.sweep_alerts()
        d.dispatch()
    assert started == [other], "a run started on the logged-out provider, or the other provider stopped"
    held = p.db.task(task["id"])
    assert held["status"] == "queued" and held["attempts"] == 0 and held["blocked_reason"].startswith(LOGGED_OUT_NOTE)
    assert p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 1, "a run checked the login"
    line = [ln for ln in cli.status_text(p).splitlines() if ln.startswith("cursor is logged out")]
    assert len(line) == 1 and "next login check" in line[0] and "`" not in line[0], line



def test_the_login_check_backs_off_and_closes_the_breaker_and_its_alert(env, monkeypatch):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import AUTH_CHECK_S, Daemon
    from ttp.providers import get_provider
    from ttp.web import attention
    d = Daemon(p.base)
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d.open_breaker("fake", "401")
    d.alert("auth:fake", "fake is logged out", "high")
    checks = []
    real = type(get_provider("fake")).login_check
    monkeypatch.setattr(type(get_provider("fake")), "login_check", lambda self: checks.append(1) or real(self))
    d.sweep_alerts()
    assert checks == [], "checked before the first backoff step"
    gaps = []
    for _ in range(len(AUTH_CHECK_S) + 1):
        b = alerts.breaker(p.db, "fake")
        p.db.set_kv("auth_breaker:fake", {**b, "next_check": time.time() - 1})
        d.sweep_alerts()
        gaps.append(round(alerts.breaker(p.db, "fake")["next_check"] - time.time()))
    assert gaps == [s for s in AUTH_CHECK_S[1:]] + [AUTH_CHECK_S[-1]] * 2, gaps
    assert len(checks) == len(AUTH_CHECK_S) + 1 and attention(p.db, time.time())
    login.write_text("")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None and not d._logged_out("fake")
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake'")["cleared"], "the alert outlived the breaker"
    assert attention(p.db, time.time()) == []
    assert p.db.one("SELECT COUNT(*) n FROM runs")["n"] == 0, "the login was checked with a model run"



@pytest.mark.parametrize("switch", [False, True])
def test_a_login_back_on_another_account_holds_one_check_then_starts_and_says_so(env, monkeypatch, switch):
    """A login that comes back as another account than before the logout may be a user mid-switch:
    nothing starts until one more check; still another account then, work starts, the chat hears
    the change (account kind only) and an event records it. The same account starts at once."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import ACCOUNT_SWITCH_EVENT, AUTH_CHECK_S, Daemon
    from ttp.providers import get_provider
    cls = type(get_provider("fake"))
    who = {"account": "one@example.com | plan_a", "kind": "plan_a"}
    monkeypatch.setattr(cls, "account", lambda self: who["account"])
    monkeypatch.setattr(cls, "account_kind", lambda self: who["kind"])
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    p.db.x("INSERT INTO runs(role,provider,account,started,ended,status) VALUES('worker','fake',?,?,?,'ok')",
           (who["account"], time.time() - 60, time.time() - 50))
    d = Daemon(p.base)
    d.open_breaker("fake", "401")
    d.alert("auth:fake", "fake is logged out", "high")
    if switch:
        who.update(account="two@example.com | plan_b", kind="plan_b")
    login.write_text("")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    events = lambda: p.db.q("SELECT * FROM events WHERE kind=?", (ACCOUNT_SWITCH_EVENT,))
    cleared = lambda: p.db.q("SELECT text FROM messages WHERE text LIKE 'Cleared: fake is logged in again%'")
    if not switch:
        assert alerts.breaker(p.db, "fake") is None, "the same account waited"
        assert not events() and len(cleared()) == 1 and "another account" not in cleared()[0]["text"]
        return
    b = alerts.breaker(p.db, "fake")
    assert b and b["account_hold"] and d._logged_out("fake"), "another account started at once"
    assert round(b["next_check"] - time.time()) == AUTH_CHECK_S[0] and not events() and not cleared()
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake"), "the hold ended before its check was due"
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None and not d._logged_out("fake")
    ev = events()
    assert len(ev) == 1 and ev[0]["status"] == "handled" and "plan_a before, plan_b now" in ev[0]["text"]
    text = cleared()[0]["text"]
    assert len(cleared()) == 1 and "another account than before the logout (plan_a before, plan_b now)" in text
    assert "@" not in text + ev[0]["text"] + ev[0]["data"], "an email address was shown or kept"



def test_a_login_back_on_the_old_account_after_its_hold_starts_without_a_note(env, monkeypatch):
    """A user who switches back within the hold's check gets the plain 'logged in again'."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import ACCOUNT_SWITCH_EVENT, Daemon
    from ttp.providers import get_provider
    cls = type(get_provider("fake"))
    who = {"account": "a | plan_a"}
    monkeypatch.setattr(cls, "account", lambda self: who["account"])
    login = env["tmp"] / "logged-in"
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d = Daemon(p.base)
    from ttp import budget as bud
    bud.note_account(p.db, "fake", who["account"])
    d.open_breaker("fake", "401")
    who["account"] = "b | plan_b"
    login.write_text("")
    for _ in range(2):
        p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
        d.check_logins()
        if alerts.breaker(p.db, "fake"):
            who["account"] = "a | plan_a"   # switched back during the hold
    assert alerts.breaker(p.db, "fake") is None
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (ACCOUNT_SWITCH_EVENT,))
    assert not (p.db.kv("auth_breaker:fake") or {}).get("switched")



def test_a_refusal_right_after_a_passed_login_check_falls_back_to_one_probe_run(env, monkeypatch):
    """A CLI whose status says logged in while the service still refuses it must not release the
    whole queue to fail again after every check."""
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    login = env["tmp"] / "logged-in"
    login.write_text("")
    monkeypatch.setenv("TTP_FAKE_LOGIN", str(login))
    d.open_breaker("fake", "401")
    p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None
    d.open_breaker("fake", "401 again")   # the next run was refused all the same
    for _ in range(2):
        p.db.set_kv("auth_breaker:fake", {**alerts.breaker(p.db, "fake"), "next_check": time.time() - 1})
        d.sweep_alerts()
    b = alerts.breaker(p.db, "fake")
    assert b and b["distrust"] and b["probe"] and d._may_probe("fake", time.time())
    # A run that gets through ends it, and the check is trusted again next time.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time(), time.time()))
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake") is None
    d.open_breaker("fake", "logged out")
    assert not alerts.breaker(p.db, "fake").get("distrust")



def test_a_logged_out_alert_from_before_the_breaker_opens_it(env):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.alert("auth:fake", "fake is logged out", "high")
    assert alerts.breaker(p.db, "fake") is None
    d.sweep_alerts()
    assert alerts.breaker(p.db, "fake"), "an upgrade dropped the logout"
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake'")["cleared"] is None



def test_only_the_three_push_queue_alerts_reach_the_top_section_and_only_while_active(env):
    from ttp import alerts
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    p, rid = _pq_rows(env)
    d = Daemon(p.base)
    now = time.time()
    for key in ("push_rejected", "after_push_failed", "push_queue_dying"):
        d.alert(key, f"{key} text", severity="high")
    # The rest is news for the coordinator or the feed, never the top section.
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (now, "daemon", "push_tip_failed", "high", "the tip fails", "queued"))
    p.db.post("out", "push batch b0 ended nothing", chat=None, kind="info", severity="normal")
    top = lambda: sorted(m["text"] for m in state_payload(p, p.db)["attention"])  # noqa: E731
    assert top() == ["after_push_failed text", "push_queue_dying text", "push_rejected text"]
    later = time.time() + 1
    _pq_batch_row(p, "b1", later, age=0, outcome="pushed", pushed_sha="cd" * 20, ended=later, finalized=later,
                  after_push="ok", after_finalized=later)
    assert top() == [], "a pushed and deployed batch clears all three"
    assert sorted(ep["key"] for ep in alerts.sweep(p.db, later + 1)) == [
        "after_push_failed", "push_queue_dying", "push_rejected"]
    feed = [m for m in state_payload(p, p.db)["feed"] if m["state"] == "cleared"]
    assert sorted(m["text"] for m in feed) == ["after_push_failed text", "push_queue_dying text", "push_rejected text"]



def test_a_long_project_pause_alerts_once_after_the_threshold_and_clears_on_resume(env):
    p = make(env)
    from ttp import alerts, projpause
    from ttp.daemon import Daemon

    def alerts_out():
        return p.db.q("SELECT * FROM messages WHERE direction='out' AND ref LIKE 'paused:%' ORDER BY id")
    p.db.set_paused(True, by="coordinator", reason="board firmware update")
    Daemon(p.base).tick()
    assert alerts_out() == []   # not before the threshold
    since = time.time() - projpause.DEFAULT_ALERT_S - 60
    p.db.set_kv(projpause.INFO_KEY, {**p.db.kv(projpause.INFO_KEY), "since": since})
    Daemon(p.base).tick()
    Daemon(p.base).tick()
    out = alerts_out()
    assert len(out) == 1 and out[0]["kind"] == "alert" and out[0]["severity"] == "normal", out
    assert "demo" in out[0]["text"] and "the coordinator" in out[0]["text"] and "board firmware update" in out[0]["text"]
    assert any(m["id"] == out[0]["id"] for m in alerts.needs_you(p.db, time.time()))
    # Resuming clears it at once, and says so once.
    p.db.set_paused(False, by="user", via="CLI")
    ep = p.db.one("SELECT * FROM alerts WHERE key=?", (out[0]["ref"],))
    assert ep["cleared"] and "resumed by the user" in ep["cleared_why"], ep
    assert not any(m["id"] == out[0]["id"] for m in alerts.needs_you(p.db, time.time()))
    Daemon(p.base).tick()
    assert [m["kind"] for m in alerts_out()] == ["alert", "resolved"]



def test_alerts_ack_of_a_live_condition_keeps_it_quiet_in_the_feed_until_it_clears(env, capsys):
    # Acking a condition that still holds must not close it: callers re-report every tick, and a
    # closed episode would page the user again at once.
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.web import attention
    p = make(env)
    d = Daemon(p.base)
    p.db.set_kv("coordinator_failures", 3)
    d.alert("coordinator", "The coordinator keeps failing", "high")
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "known; fixing the provider"])
    assert "kept in the feed" in capsys.readouterr().out
    ep = _episodes(p, "coordinator")[0]
    assert ep["cleared"] is None and alerts.acked(p.db, ep)["why"] == "known; fixing the provider"
    for every_s in (6 * 3600, 0):
        d.alert("coordinator", "The coordinator keeps failing", "high", every_s=every_s)
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 1, "the ack paged the user again"
    assert attention(p.db, time.time()) == []
    feed = [m for m in alerts.feed(p.db, time.time()) if m["id"] == mid]
    assert feed and feed[0]["state"] == "acked" and feed[0]["acked_why"] == "known; fixing the provider"
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "a retry"])   # keeps the first receipt
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["why"] == "known; fixing the provider"
    d.alert("coordinator", "The coordinator is down", "critical")   # a raised severity is new
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 2
    p.db.set_kv("coordinator_failures", 0)
    [closed] = alerts.sweep(p.db)
    assert closed["cleared"] and "acknowledged: known; fixing the provider" in closed["cleared_why"]
    assert p.db.kv(alerts.ACKS) == {}
    p.db.set_kv("coordinator_failures", 1)
    d.alert("coordinator", "The coordinator fails again", "high")
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 3, "a new occurrence alerts again"
    assert [m["text"] for m in attention(p.db, time.time())] == ["The coordinator fails again"]



def test_alerts_ack_covers_only_its_severity_and_a_raise_surfaces_again(env, monkeypatch):
    # An ack of a high alert must not hide the same episode once it escalates to critical.
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.web import attention
    p = make(env)
    d = Daemon(p.base)
    p.db.set_kv("coordinator_failures", 3)
    d.alert("coordinator", "x", "high")
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "known"])
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["severity"] == "high"
    d.alert("coordinator", "DOWN", "critical")
    assert [m["text"] for m in attention(p.db, time.time())] == ["DOWN"]
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0]) is None
    down = p.db.one("SELECT id FROM messages WHERE kind='alert' AND text='DOWN'")["id"]
    assert [m["state"] for m in alerts.feed(p.db, time.time()) if m["id"] == down] != ["acked"]
    d.alert("coordinator", "DOWN", "critical")   # a repeat dedups as usual, nothing quietly dropped
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 2
    later = time.time() + alerts.REMIND_S + 60
    monkeypatch.setattr(time, "time", lambda: later)
    d.alert("coordinator", "DOWN", "critical")   # and reminds once REMIND_S has passed
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 3
    cli.main(["alerts", "demo", "--ack", str(down), "--why", "on it"])   # a fresh ack covers the raise
    assert alerts.acked(p.db, _episodes(p, "coordinator")[0])["severity"] == "critical"
    assert attention(p.db, later) == []



def test_alerts_ack_of_an_outage_that_is_over_closes_it_and_releases_its_claim(env):
    from ttp import alerts, cli
    from ttp.daemon import Daemon
    from ttp.project import hostname
    p = make(env)
    Daemon(p.base).alert("auth:fake", "fake is logged out", "high")
    claim = f"{hostname()}|auth:fake"
    assert claim in json.loads(alerts.claims_path().read_text())
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 1))   # logged in again, before any sweep
    mid = p.db.one("SELECT id FROM messages WHERE kind='alert'")["id"]
    cli.main(["alerts", "demo", "--ack", str(mid), "--why", "logged back in"])
    assert _episodes(p, "auth:fake")[0]["cleared"]
    assert claim not in json.loads(alerts.claims_path().read_text()), "the ack leaked the machine-wide claim"



def test_top_section_keeps_old_open_asks_and_drops_keyless_alerts_after_an_hour(env):
    p = make(env)
    from ttp.alerts import needs_you
    now = time.time()
    old = p.db.post("out", "Old question?", kind="ask", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 30 * 86400, old))
    for i in range(310):   # many alerts must not push the ask out
        p.db.post("out", f"noise {i}", kind="alert", severity="low")
    stale = p.db.post("out", "Keyless old", kind="alert", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 2 * 3600, stale))
    p.db.post("out", "Relabelled", kind="alert_old", severity="high")
    p.db.post("out", "Keyless fresh", kind="alert", severity="high")
    texts = [m["text"] for m in needs_you(p.db, now)]
    assert texts == ["Old question?", "Keyless fresh"], texts
    from ttp.web import health
    assert [a["text"] for a in health(p, p.db, now=now)["asks"]] == ["Old question?"]



def test_a_working_logged_out_probe_releases_the_coordinator_and_queue_before_it_ends(env, monkeypatch):
    """The user logged in and the probe runs for real: waiting for it to end would hold every other
    run (and unanswered messages) for up to the probe's whole run timeout."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")   # the logout came first
    # A run from before the logout, still spending, says nothing about the login now.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',1.0)",
           (now - 7200,))
    p.db.set_kv("auth_probe:fake", now - 30)
    run = p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',0)",
                 (now - 30,))
    p.db.post("in", "I logged in, status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    # Seconds in and nothing spent yet: still the one probe.
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started == ["small"], started
    # It spends tokens: the login works, everything starts on the same tick.
    p.db.x("UPDATE runs SET cost_usd=0.4 WHERE id=?", (run,))
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started[:2] == ["small", "coordinator"] and sorted(started[2:]) == ["big", "other"], started
    # Output tokens count too, priced or not; age alone does not.
    p.db.x("UPDATE runs SET cost_usd=0, started=? WHERE id=?", (time.time() + 1, run))
    assert alerts.holds(p.db, "auth:fake", time.time(), time.time() + 3600)
    p.db.x("UPDATE runs SET output_tokens=12 WHERE id=?", (run,))
    assert not alerts.holds(p.db, "auth:fake", time.time(), time.time() + 5)



def test_a_hung_logged_out_probe_holds_the_queue_until_it_shows_spend(env, monkeypatch, tmp_path):
    """A provider CLI that hangs or retries while still logged out must not release the queue just
    by running long: every queued run would then fail once on the logout. Through full ticks."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")
    p.db.set_kv("auth_breaker:fake", {**p.db.kv("auth_breaker:fake"), "opened": now - 3600})
    p.db.set_kv("auth_probe:fake", now - 600)
    run_dir = tmp_path / "probe"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    (run_dir / "output.jsonl").write_text(json.dumps({"error": "retrying"}))   # output, but nothing spent
    run = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id) VALUES('worker','fake',?,'running',?,?)",
                 (now - 600, str(run_dir), d.boot))
    p.db.post("in", "status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    for _ in range(3):
        d.tick()
    assert started == ["small"], "a probe hung past the old grace released the queue"
    assert alerts.holds(p.db, "auth:fake", now - 3600, time.time() + 3600)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"] is None
    # It starts spending: the login works and everything starts on the next tick.
    (run_dir / "output.jsonl").write_text(json.dumps({"_cost": 0.3, "_output_tokens": 40}))
    (run_dir / "lease").touch()
    d._metered.clear()
    d.tick()
    row = p.db.one("SELECT cost_usd, output_tokens FROM runs WHERE id=?", (run,))
    assert (row["cost_usd"], row["output_tokens"]) == (0.3, 40), row
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"]
    assert "coordinator" in started and {"big", "other"} <= set(started), started



def test_boot_check_restores_an_empty_memory_file_and_a_truncated_index(env):
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon, KV_INTEGRITY
    a = p.add_memory("The build needs the release toolchain.", kind="fact")
    p.add_memory("Reviews go before every push.", kind="decision")
    good_entry, good_index = a.read_bytes(), p.memory_index.read_bytes()
    a.write_bytes(b"")                                        # what a cut write leaves
    p.memory_index.write_bytes(good_index[:len(good_index) // 2])
    stray = p.memory_dir / "fact-never-committed.md"
    stray.write_bytes(b"\0" * 64)
    d = Daemon(p.base)
    t0 = time.monotonic()
    d.check_integrity(start=True)
    took = time.monotonic() - t0
    assert a.read_bytes() == good_entry
    assert p.memory_index.read_bytes() == good_index
    assert not stray.exists() and (p.state / "damaged" / stray.name).exists()
    res = p.db.kv(KV_INTEGRITY)
    assert res["fsck"] == "ok" and not res["bad"] and len(res["restored"]) == 3, res
    assert took < 2, f"boot check took {took:.2f} s"
    assert p.db.one("SELECT id FROM messages WHERE kind='info' AND text LIKE '%repaired%'")
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert'")
    clean = subprocess.run(["git", "-C", str(p.harness), "status", "--porcelain"], capture_output=True, text=True)
    assert clean.stdout == "", clean.stdout
    # The same boot is not checked again; nothing was left broken.
    at = res["at"]
    d.check_integrity(start=True)
    d.check_integrity()
    assert p.db.kv(KV_INTEGRITY)["at"] == at
    assert not alerts.holds(p.db, "integrity", at, time.time())



def test_boot_check_alerts_once_on_what_it_cannot_repair_and_clears(env, monkeypatch):
    p = make(env)
    from ttp import alerts, integrity
    from ttp.daemon import Daemon, KV_INTEGRITY
    p.add_memory("A fact.")
    blob = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD:MEMORY.md"], capture_output=True,
                          text=True).stdout.strip()
    obj = p.harness / ".git" / "objects" / blob[:2] / blob[2:]
    obj.rename(obj.with_name("gone"))
    tid = p.db.add_task("code", "s", kind="code", tier="light", origin="user")
    wt = p.worktrees / f"t{tid}"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {env['tmp'] / 'nowhere'}\n")
    fscked = []
    real = integrity._git
    monkeypatch.setattr(integrity, "_git", lambda cwd, *a, **kw: (fscked.append(cwd) if a[0] == "fsck" else None)
                        or real(cwd, *a, **kw))
    d = Daemon(p.base)
    d.check_integrity(start=True)
    res = p.db.kv(KV_INTEGRITY)
    assert res["fsck"] == "failed" and res["bad"] and res["worktrees"][0]["task"] == tid, res
    assert fscked == [p.harness], "only the harness is fsck'd, never a code repository"
    al = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity'")
    assert len(al) == 1 and f"task #{tid}" in al[0]["text"]
    assert alerts.holds(p.db, "integrity", al[0]["ts"], time.time())
    # Repaired by hand: the next start checks again and the alert clears.
    obj.with_name("gone").rename(obj)
    (wt / ".git").unlink()
    wt.rmdir()
    d.check_integrity(start=True)
    assert not alerts.holds(p.db, "integrity", al[0]["ts"], time.time())
    assert len(p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='integrity'")) == 1



def test_pr_watch_records_a_pr_the_user_took_out_of_draft_as_their_approval(env, monkeypatch):
    """The user and the harness share one GitHub account. A PR that leaves draft with no run's gh call
    behind it was the user's own doing: it is recorded as their approval, with one low feed line and
    no high alert, and nothing on the PR is changed."""
    from ttp import alerts, coordinator as coord, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    old, new = "https://github.com/acme/widgets/pull/3", "https://github.com/acme/widgets/pull/7"
    for n, url in ((1, old), (2, new)):
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               (f"t{n}", "code", "done", url, time.time()))
    state = {old: {"isDraft": False}, new: {"isDraft": True}}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" else
                        {"url": args[2], "state": "OPEN", "title": "t", "headRefOid": HEAD, **state[args[2]]})
    # PRs out of draft before this check existed are not flagged.
    p.db.set_kv("pr_signatures", {old: {"state": "OPEN", "draft": False}})
    d = Daemon(p.base)

    def alerting():
        return [m["text"] for m in p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'pr-ready:%'")
                if alerts.active(p.db, m["ref"], m["ts"], time.time())]

    watchers.watch_prs(d)
    assert alerting() == [] and not p.db.q("SELECT id FROM events WHERE kind LIKE 'pr_%ready%'")
    # A run's call on another PR long before #7 was last seen in draft says nothing about #7.
    _run_tried(p, "acme/widgets#8")
    _run_tried(p, "acme/widgets#7", ago=watchers.DAY)
    state[new]["isDraft"] = False      # the user took #7 out of draft on GitHub
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    assert alerting() == [] and p.db.kv(prguard.UNAPPROVED_KEY) == {}
    assert not p.db.q("SELECT id FROM events WHERE kind='pr_unapproved_ready'")
    ev = p.db.q("SELECT text, severity, status FROM events WHERE kind='pr_ready_by_user'")
    assert len(ev) == 1 and new in ev[0]["text"] and ev[0]["severity"] == "low" and ev[0]["status"] == "handled"
    rec = p.db.kv(prguard.APPROVALS_KEY)["acme/widgets#7"]
    assert rec["channel"] == prguard.GITHUB and rec["head"] == HEAD and rec["spent"], rec
    # It is the user's approval for that time only: a run still may not mark it ready with it...
    assert prguard.approved(p.db, "acme/widgets#7") and not prguard.may_ready(p.db, "acme/widgets#7")
    # ...and back in draft it is gone, so leaving draft again is judged afresh.
    state[new]["isDraft"] = True
    watchers.watch_prs(d)
    assert "acme/widgets#7" not in (p.db.kv(prguard.APPROVALS_KEY) or {})
    state[old]["isDraft"] = True
    watchers.watch_prs(d)
    state[old]["isDraft"] = False      # #3 was grandfathered only until it went back to draft
    _run_tried(p, "acme/widgets#3")
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#3"}



def test_pr_watch_alerts_when_a_run_took_a_pr_out_of_draft(env, monkeypatch):
    """A PR left draft without approval and the runs' gh log shows a run marking it ready (or
    requesting reviewers, or a call whose PR it could not tell): a high alert names the run, the
    coordinator hears it once, and the PR is not put back in draft."""
    from ttp import alerts, coordinator as coord, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = {n: f"https://github.com/acme/widgets/pull/{n}" for n in (7, 8, 9)}
    for url in urls.values():
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    draft = {url: True for url in urls.values()}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" else
                        {"url": args[2], "state": "OPEN", "title": "t", "headRefOid": HEAD, "isDraft": draft[args[2]]})
    p.db.set_kv("pr_signatures", {})
    d = Daemon(p.base)

    def alerting():
        return sorted((m["ref"], m["text"]) for m in p.db.q("SELECT * FROM messages WHERE kind='alert' "
                                                           "AND ref LIKE 'pr-ready:%'")
                      if alerts.active(p.db, m["ref"], m["ts"], time.time()))

    watchers.watch_prs(d)
    _run_tried(p, "acme/widgets#7", run="41", task="5")                       # refused, then got around it
    _run_tried(p, "acme/widgets#8", action="reviewers", refused=False, run="42", task="6")
    _run_tried(p, None, run="43", task="6")                                   # its PR could not be identified
    for url in urls.values():
        draft[url] = False
    watchers.watch_prs(d)
    watchers.watch_prs(d)
    shown = alerting()
    assert [r for r, _ in shown] == [f"pr-ready:acme/widgets#{n}" for n in (7, 8, 9)]
    assert "run 41 of task #5 marked it ready" in shown[0][1] and "refused" in shown[0][1]
    assert "run 42 of task #6 requested reviewers" in shown[1][1] and "run 43" in shown[2][1]
    assert all("Nothing on the PR was changed" in t for _, t in shown)
    ev = p.db.q("SELECT text, severity, status FROM events WHERE kind='pr_unapproved_ready' ORDER BY id")
    assert len(ev) == 3 and all(e["severity"] == "high" and e["status"] == "queued" for e in ev), "told once each"
    assert "run 41" in ev[0]["text"] and "never will" in ev[0]["text"]
    assert not p.db.q("SELECT id FROM events WHERE kind='pr_ready_by_user'")
    assert not (p.db.kv(prguard.APPROVALS_KEY) or {}), "a run's doing is never the user's approval"
    # The user approves #7 after the fact: its alert clears at once.
    said = p.db.post("in", f"yes, {urls[7]} can stay ready", chat="web", provenance="web-session")
    assert coord.apply(p, [{"type": "pr_approve", "id": said, "text": urls[7], "quote": "can stay ready"}]) == []
    assert [r for r, _ in alerting()] == ["pr-ready:acme/widgets#8", "pr-ready:acme/widgets#9"]
    alerts.sweep(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='pr-ready:acme/widgets#7'")["cleared"]
    # The user puts #8 back in draft themselves: its flag goes.
    draft[urls[8]] = True
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#9"}



def test_pr_watch_drops_flags_for_cancelled_tasks_and_prs_it_cannot_read(env, monkeypatch):
    """A flagged PR whose task was cancelled, or that gh has not been able to read for a day, no longer
    holds its high alert."""
    from ttp import alerts, prguard, watchers
    from ttp.daemon import Daemon
    p = make(env)
    a, b = "https://github.com/acme/widgets/pull/7", "https://github.com/acme/widgets/pull/8"
    ids = [p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
                  ("t", "code", "done", url, time.time())) for url in (a, b)]
    readable = {a: True, b: True}
    monkeypatch.setattr(watchers, "_gh", lambda args, cwd: None if args[0] == "api" or not readable[args[2]] else
                        {"url": args[2], "state": "OPEN", "title": "t", "isDraft": False})
    p.db.set_kv("pr_signatures", {})
    _run_tried(p, "acme/widgets#7")
    _run_tried(p, "acme/widgets#8")
    d = Daemon(p.base)

    def alerting():
        return sorted(m["ref"] for m in p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref LIKE 'pr-ready:%'")
                      if alerts.active(p.db, m["ref"], m["ts"], time.time()))

    watchers.watch_prs(d)
    assert alerting() == ["pr-ready:acme/widgets#7", "pr-ready:acme/widgets#8"]
    # #7's task is cancelled: its flag goes on the next pass.
    p.db.x("UPDATE tasks SET status='cancelled' WHERE id=?", (ids[0],))
    # gh cannot read #8: the flag stays for a day, then goes.
    readable[b] = False
    watchers.watch_prs(d)
    assert set(p.db.kv(prguard.UNAPPROVED_KEY)) == {"acme/widgets#8"}
    assert alerting() == ["pr-ready:acme/widgets#8"]
    flags = p.db.kv(prguard.UNAPPROVED_KEY)
    flags["acme/widgets#8"]["seen"] -= watchers.DAY + 1
    p.db.set_kv(prguard.UNAPPROVED_KEY, flags)
    watchers.watch_prs(d)
    assert p.db.kv(prguard.UNAPPROVED_KEY) == {} and alerting() == []



def test_pr_watch_stops_at_its_deadline_keeps_what_it_read_and_resumes_there(env, monkeypatch):
    """Slow gh calls: a builtin run stops at its total deadline, keeps the PRs it read, reports one error
    that it was cut short (one issue however many runs are cut), and the next run starts at the PR it
    did not reach. A whole run closes the error."""
    from ttp import screen as scr, watchers
    from ttp.daemon import Daemon
    p = make(env)
    urls = [f"https://github.com/acme/widgets/pull/{n}" for n in range(1, 6)]
    for url in urls:
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", url, time.time()))
    clock, step, viewed = [1000.0], [30.0], []
    monkeypatch.setattr(watchers, "_now", lambda: clock[0])

    def slow_gh(args, cwd):
        clock[0] += step[0]   # each call takes `step` seconds
        if args[0] == "api":
            return None
        viewed.append(args[2])
        return {"url": args[2], "state": "OPEN", "title": "t", "isDraft": True, "headRefOid": HEAD}
    monkeypatch.setattr(watchers, "_gh", slow_gh)
    d = Daemon(p.base)
    errors = lambda: p.db.q("SELECT status, count FROM issues WHERE source=?",
                            (scr.error_source("watcher:prs"),))
    status = watchers.run_builtin(d, "prs", {"deadline_s": 90})
    # Two PRs (a view and a GraphQL read each) take 120 s: the third is not started.
    assert status.startswith("cut short (2 of 5 PRs read"), status
    assert viewed == urls[:2] and set(p.db.kv("pr_signatures")) == set(urls[:2])
    assert [e["status"] for e in errors()] == ["open"]
    status = watchers.run_builtin(d, "prs", {"deadline_s": 90})
    assert status.startswith("cut short") and viewed[2:] == urls[2:4]
    assert [e["status"] for e in errors()] == ["open"], "a second cut run is the same error"
    step[0] = 0.0
    assert watchers.run_builtin(d, "prs", {"deadline_s": 90}).startswith("ok (5 PRs")
    assert viewed[4:] == [urls[4], *urls[:4]], "it starts at the PR the cut run did not reach"
    assert [e["status"] for e in errors()] == ["fixed"]
    assert p.db.kv(watchers.PR_CURSOR_KEY) is None


def test_a_hanging_gh_call_is_killed_at_the_watchers_deadline(env, monkeypatch, tmp_path):
    """A gh that never answers holds a builtin run no longer than its deadline, whatever its per-call
    timeout, and the run reports itself cut short."""
    from ttp import watchers
    from ttp.daemon import Daemon
    p = make(env)
    for n in (1, 2, 3):
        p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
               ("t", "code", "done", f"https://github.com/acme/widgets/pull/{n}", time.time()))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/sh\nexec sleep 30\n")
    (bindir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    d = Daemon(p.base)
    t0 = time.monotonic()
    status = watchers.run_builtin(d, "prs", {"deadline_s": 1.5, "gh_timeout_s": 20})
    assert time.monotonic() - t0 < 6, "the tick stays bounded by the deadline"
    assert status.startswith("cut short (0 of 3 PRs read"), status


def test_builtin_watcher_bounds_come_from_the_payload_then_the_config_and_are_capped(env):
    from ttp import watchers
    from ttp.daemon import Daemon
    d = Daemon(make(env).base)
    assert watchers.bounds(d, {}) == (watchers.DEADLINE_S, watchers.GH_TIMEOUT_S)
    d.cfg = {**d.cfg, "watchers": {"deadline_s": 50, "gh_timeout_s": 10}}
    assert watchers.bounds(d, {}) == (50, 10)
    assert watchers.bounds(d, {"deadline_s": 5}) == (5, 5), "a call never outlasts the deadline"
    assert watchers.bounds(d, {"deadline_s": 10_000, "gh_timeout_s": "x"}) == (watchers.DEADLINE_MAX_S, 10)



def test_the_global_cap_alert_clears_by_itself_at_the_day_reset(env, tmp_path, monkeypatch):
    p, other = _global_cap_setup(env, tmp_path)
    from ttp import alerts
    from ttp import globalcap as gcap
    from ttp.daemon import Daemon
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    d = Daemon(p.base)
    d.update_gates()
    other.spend("fake", 12.0, "task:1")
    clock[0] += gcap.LOCAL_CACHE_S + 1      # the other projects' totals are read again
    d.update_gates()
    alert = p.db.one("SELECT ts, text FROM messages WHERE ref='budget:fake'")
    assert alert and "starts again by itself when the budget day resets" in alert["text"], alert
    assert alerts.holds(p.db, "budget:fake", alert["ts"], clock[0])
    clock[0] = gcap.day_bounds(p.config()["budget"], clock[0])[1] + 60
    d.update_gates()
    assert d.gates["fake"].level != "red"
    assert not alerts.holds(p.db, "budget:fake", alert["ts"], clock[0])



NOISY_STDERR = {"401": "MCP server tools: HTTP 401 Unauthorized\n", "400": "MCP server tools: HTTP 400 Bad Request\n"}



REJECTED_LIMIT_EVENT = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}



NOISE_CASES = {**{f"{prov}-{code}": (prov, OK_RUNS[prov], line) for prov in OK_RUNS for code, line in NOISY_STDERR.items()},
               "claude-rejected-limit-event": ("claude", [REJECTED_LIMIT_EVENT, *OK_RUNS["claude"]], "")}



@pytest.mark.parametrize("case", list(NOISE_CASES))
def test_a_successful_priced_run_with_noisy_stderr_or_a_rejected_limit_event_stays_ok(env, case, tmp_path):
    # Replays the probe cases of a review that found a shared classifier marking these runs auth,
    # limit or a request error: the parse, then the run's status, the breaker and the limit hold.
    provider, lines, stderr = NOISE_CASES[case]
    u = _parsed(provider, tmp_path, lines, stderr)
    assert not (u.auth_failed or u.limited or u.request_error) and (u.cost_usd or u.output_tokens), u
    p = make(env)
    from ttp import alerts
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    run, _ = _finished_run(d, lines, provider=provider, stderr=stderr, rc=0)
    assert run["status"] == "ok", run["status"]
    assert not alerts.breaker(p.db, provider) and p.db.kv(f"limited:{provider}") is None
    assert not p.db.q("SELECT id FROM messages WHERE ref IN (?, ?)", (f"auth:{provider}", f"limit:{provider}"))



@pytest.mark.parametrize("provider", list(REJECTED_RUNS))
def test_a_failed_run_with_a_typed_schema_400_fails_its_task_at_once_without_a_raised_retry(env, provider):
    p = make(env)
    from ttp import alerts, effort
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    run, task = _finished_run(d, REJECTED_RUNS[provider], provider=provider, tier="light")
    assert run["status"] == "request_error", run["status"]
    assert task["status"] == "failed" and task["attempts"] == 1, "a schema rejection was queued for a retry"
    result = json.loads(task["result"])
    assert result["status"] == "request_error" and "400" in result["summary"], result
    assert effort.retry_tier(p.db, task) is None, "a requeued schema rejection would retry at a raised tier"
    assert not alerts.breaker(p.db, provider) and p.db.kv(f"limited:{provider}") is None



def test_a_machine_outage_alert_holds_while_reported_and_clears_with_its_condition(env):
    from ttp import alerts
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp.daemon import Daemon
    alpha = make(env, "alpha")
    mm.add("box-a", tags="device")
    now = time.time()
    ml.report("gamma", "box-a", "down", now=now)
    d = Daemon(alpha.base)
    d._machine_page({"target": ml.USER, "kind": "machine_condition", "key": "machine:box-a:down",
                     "alias": "box-a", "text": "Machine box-a down."}, now)
    late = now + alerts.DAY + 3600
    ml.report("gamma", "box-a", "down", now=late)
    assert alerts.sweep(alpha.db, late) == [], "still reported past a day: it holds"
    ml.report("gamma", "box-a", "down", cleared=True)
    closed = alerts.sweep(alpha.db, late)
    assert [(c["key"], c["cleared_why"]) for c in closed] == [("machine:box-a:down", "condition cleared")]
    assert alpha.db.one("SELECT text FROM messages WHERE kind='resolved' AND ref='machine:box-a:down'")
    # Nobody reporting it for CONDITION_GONE_S ends it too.
    ml.report("gamma", "box-a", "held", now=now)
    d.alert("machine:box-a:held", "Outage: Machine box-a held.", severity="high")
    assert alerts.sweep(alpha.db, now + 60) == []
    assert [c["key"] for c in alerts.sweep(alpha.db, now + ml.CONDITION_GONE_S)] == ["machine:box-a:held"]
    # An overdue change: holds while it is open and overdue, clears once it is closed.
    c = ml.add_change("box-a", "drop-in", "rm it", "gamma", expires="1h", now=now - 7200)
    key = f"machine-change:{c['id']}"
    d.alert(key, "Outage: Machine change overdue.", severity="high")
    assert alerts.sweep(alpha.db, late) == []
    ml.close_change(c["id"], "undone")
    assert [x["key"] for x in alerts.sweep(alpha.db, late)] == [key]



def test_a_los_angeles_project_on_a_utc_box_shows_pdt_or_pst_in_every_surface(env, utc_box):
    """The process runs on UTC, the project's home zone is America/Los_Angeles: ttp status, the web
    app's JSON and its client-side clock, alert and wait texts and the coordinator's digest (what its
    chat replies are written from) all show Pacific time with its abbreviation, never UTC."""
    import shutil
    from ttp import alerts, anchors, waits, web
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.db import host_line
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    db, now = p.db, time.time()
    pac = re.compile(r"\d\d:\d\d P[DS]T")
    db.set_kv("coordinator_failures", 2)
    db.set_kv("coordinator_backoff_until", now + 600)
    db.set_kv("limited:fake", {"until": now + 900, "note": "logged out"})
    tid = db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user",
                      not_before=now + 1200)
    # ttp status: the clock in the head line, retry and pause times.
    out = status_text(p)
    for need in ("retry at", "fake paused until", "next try"):
        line = next(ln for ln in out.splitlines() if need in ln)
        assert pac.search(line.split(need, 1)[1]) and " UTC" not in line, line
    # The web app's JSON: the zone it formats in, and the lines the daemon writes for it.
    h = web.health(p, db, now=now)
    assert h["zone"] == "America/Los_Angeles" and web.state_payload(p, db)["project"]["zone"] == h["zone"]
    assert pac.search(h["why_idle"].split("next try", 1)[1]), h["why_idle"]
    assert pac.search(web.gate_detail({"regime": "windows", "numbers": {"window": "5h", "utilization": 4,
                                       "resets_at": now + 3600, "limit": 90}}, now, p))
    db.set_kv(alerts.BREAKER + "fake", {"open": True, "opened": now - 600, "next_check": now + 600})
    line = web.breaker_lines(db, now, p)[0]["line"]
    assert len(pac.findall(line)) == 2 and " UTC" not in line, line
    assert web.at(now + 60, now).endswith("UTC")    # no zone given: UTC, never the box's or a guess
    assert pac.search(host_line([{"ts": now - 60, "lost": []}], p))
    # Alert and wait texts with clock times.
    w = waits.wait_kind({"id": tid, "status": "blocked", "labels": json.dumps([f"waits:time:{now + 900:.0f}"]),
                         "blocked_reason": "", "updated": now}, [], now, p)
    assert re.search(r"P[DS]T$", w["text"]), w
    assert re.search(r"P[DS]T$", anchors.describe("until", str(now + 900), p))
    db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(coord.defer_labels(now + 3600, None)), tid))
    db.x("UPDATE tasks SET not_before=? WHERE id=?", (now + 3600, tid))
    assert pac.search(coord.starts_text(db.task(tid), now, plain=True, where=p))
    # The coordinator's digest, which its chat replies are written from, is stamped in the home zone.
    head = coord.digest(p, {}, [], []).splitlines()[0]
    assert re.match(r"# STATE at \d{4}-\d\d-\d\d \d\d:\d\d P[DS]T ", head), head
    # The web app formats on the client in the zone the daemon sends, not the browser's.
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "toLocaleTimeString" not in js and "toLocaleString" not in js
    assert "ZONE = (st.project && st.project.zone)" in js
    node = shutil.which("node")
    if node:
        block = js[js.index("let ZONE"):js.index("\n", js.index("const at = "))]
        r = subprocess.run([node, "-e", block + "\nZONE = 'America/Los_Angeles';"
                            f"console.log(at({now + 60}), '|', at({now + 3 * 86400}))"],
                           capture_output=True, text=True, timeout=60, env={**os.environ, "TZ": "UTC"})
        assert r.returncode == 0, r.stderr
        assert re.fullmatch(r"\d\d:\d\d P[DS]T \| \w{3},? \d\d:\d\d P[DS]T\n", r.stdout), r.stdout



def test_network_outage_alerts_once_after_the_grace_in_the_home_zone_and_shows_in_the_top_section(env, monkeypatch):
    from ttp import alerts, timefmt
    from ttp.cli import status_text
    p, d, tids, now, check = _net_setup(env, monkeypatch)
    assert check(now) == [], "the grace starts at the first check that sees the outage"
    assert check(now + 600) == [], "within the grace (15 min by default) nothing is said"
    sent = check(now + 900)
    assert len(sent) == 1, "every task that could start waits on an unreachable host: one alert"
    m = sent[0]
    assert m["severity"] == "normal" and m["chat"] is None and m["channel"] == "chat", m
    assert "jump.example" in m["text"] and "2 queued task(s)" in m["text"], m["text"]
    assert "VPN or network sign-in" in m["text"] and "resumes by itself" in m["text"], m["text"]
    assert timefmt.long(now, "America/Los_Angeles") in m["text"] and ("PDT" in m["text"] or "PST" in m["text"])
    assert "ttp " not in m["text"], "no command the harness could run itself"
    assert [x["id"] for x in alerts.needs_you(p.db, now + 900)] == [m["id"]], "a normal alert, yet in the top section"
    assert "needs you (alert" in status_text(p) and "VPN" in status_text(p)
    for at in (now + 1800, now + 3600, now + 6 * 3600, now + 30 * 3600):
        assert len(check(at)) == 1, "no repeat while the outage lasts, not even a daily reminder"
    assert not p.db.q("SELECT id FROM events WHERE status IN ('new','queued')"), "the coordinator is not woken"
    assert p.db.one("SELECT COUNT(*) n FROM alerts WHERE key='network:unreachable'")["n"] == 1



def test_network_outage_clears_once_hosts_answer_with_a_low_feed_line_and_a_brief_reconnect_stays_one(env, monkeypatch):
    from ttp import alerts
    p, d, tids, now, check = _net_setup(env, monkeypatch)
    check(now)
    assert len(check(now + 900)) == 1
    since = p.db.kv("network_outage")["since"]
    # A brief reconnect: the probes pass, then fail again within the debounce (600 s by default).
    assert len(check(now + 960, rc=None)) == 1
    assert alerts.needs_you(p.db, now + 960), "the alert stays through the debounce"
    assert len(check(now + 1300)) == 1 and p.db.kv("network_outage")["since"] == since, "the same outage"
    # Reachability is back for good: the probes passed and the tasks started.
    t0 = now + 1400
    assert len(check(t0, rc=None)) == 1
    assert len(check(t0 + 300, rc=None)) == 1 and alerts.needs_you(p.db, t0 + 300)
    assert len(check(t0 + 660, rc=None)) == 1, "clearing posts no new alert"
    assert not alerts.needs_you(p.db, t0 + 660), "cleared once the hosts answered for the debounce"
    done = p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='network:unreachable'")
    assert len(done) == 1 and done[0]["severity"] == "low", "one low feed line, not a notification"
    assert not p.db.kv("network_outage")
    assert [x for x in alerts.feed(p.db, t0 + 660) if x.get("state") == "cleared"]
    # A later outage is a new one: alerted once more after its own grace.
    t1 = t0 + 3600
    assert len(check(t1)) == 1 and len(check(t1 + 600)) == 1
    assert len(check(t1 + 900)) == 2



TUNNEL_PROBE = "ssh -o ConnectTimeout=5 box-a true"



TUNNEL_OUT = "ssh: connect to host 127.0.0.1 port 2222: Connection refused"



def test_network_outage_on_a_tunnel_route_is_a_low_feed_line_raised_once_after_an_hour_then_clears(env, monkeypatch):
    import re
    from ttp import alerts
    from ttp.cli import status_text
    p, d, tids, now, check = _net_setup(env, monkeypatch, probe=TUNNEL_PROBE, out=TUNNEL_OUT,
                                        ssh_config={"box-a": {"hostname": "127.0.0.1", "port": "2222"}})
    check(now)
    sent = check(now + 900)
    assert len(sent) == 1, "one line per outage, after the same grace"
    m = sent[0]
    assert m["severity"] == "low", "a tunnel that reconnects by itself pages nobody at first"
    assert "box-a" in m["text"] and "tunnel from the user's workstation" in m["text"], m["text"]
    assert "reconnects by itself" in m["text"] and "VPN" not in m["text"] and "sign-in" not in m["text"], m["text"]
    assert not re.search(r"127\.\d+\.\d+\.\d+|::1|localhost", m["text"]), m["text"]
    assert "nothing else can start" not in m["text"] and "the project keeps running" in m["text"], m["text"]
    assert not alerts.needs_you(p.db, now + 900) and "needs you" not in status_text(p)
    assert [x["state"] for x in alerts.feed(p.db, now + 900) if x["id"] == m["id"]] == ["status"]
    assert p.db.one("SELECT severity FROM alerts WHERE key='network:unreachable'")["severity"] == "low"
    assert not p.db.unread_for_chat("c", 0), "no chat page for a low line"
    assert len(check(now + 3000)) == 1, "quiet before the threshold (60 min into the outage)"
    sent = check(now + 3600)
    assert len(sent) == 2, "raised once at the threshold"
    r = sent[1]
    assert r["severity"] == "normal" and "box-a" in r["text"] and "(1.0 h so far)" in r["text"], r["text"]
    assert "VPN" not in r["text"] and not re.search(r"127\.\d+\.\d+\.\d+", r["text"]), r["text"]
    assert [x["id"] for x in alerts.needs_you(p.db, now + 3600)] == [r["id"]]
    assert m["id"] not in [x["id"] for x in alerts.feed(p.db, now + 3600)], "the raised line replaces the low one"
    for at in (now + 7200, now + 6 * 3600):
        assert len(check(at)) == 2, "no repeat after the one raise"
    assert p.db.one("SELECT COUNT(*) n FROM alerts WHERE key='network:unreachable'")["n"] == 1, "one issue"
    # The tunnel is back: the outage clears under the same key.
    t0 = now + 6 * 3600 + 300
    check(t0, rc=None)
    assert len(check(t0 + 660, rc=None)) == 2 and not alerts.needs_you(p.db, t0 + 660)
    done = p.db.q("SELECT * FROM messages WHERE kind='resolved' AND ref='network:unreachable'")
    assert len(done) == 1 and done[0]["severity"] == "low"
    assert not p.db.kv("network_outage")



def test_network_outage_on_a_tunnel_route_that_comes_back_before_the_threshold_never_pages(env, monkeypatch):
    from ttp import alerts
    p, d, tids, now, check = _net_setup(env, monkeypatch, probe=TUNNEL_PROBE, out=TUNNEL_OUT,
                                        ssh_config={"box-a": {"hostname": "127.0.0.1", "port": "2222"}})
    check(now)
    assert [m["severity"] for m in check(now + 900)] == ["low"]
    check(now + 1020, rc=None)   # back 17 min into the outage
    assert [m["severity"] for m in check(now + 1700, rc=None)] == ["low"], "never raised"
    assert not p.db.kv("network_outage") and not alerts.needs_you(p.db, now + 1700)
    assert [x["state"] for x in alerts.feed(p.db, now + 1700) if x["kind"] == "alert"] == ["cleared"]
    # 0: page at once.
    d.cfg = {**d.cfg, "waiting": {**(d.cfg.get("waiting") or {}), "tunnel_outage_page_after_s": 0}}
    t1 = now + 7200
    check(t1)
    sent = check(t1 + 900)
    assert [m["severity"] for m in sent] == ["low", "normal"] and "tunnel" in sent[1]["text"]
    assert len(check(t1 + 4 * 3600)) == 2



def test_network_outage_sees_real_start_when_probe_verdicts_and_clears_once_they_pass(env, monkeypatch):
    """No verdict written by hand: two tasks deferred on a start_when probe that, like ssh to a host
    out of reach, prints ssh's error and exits 255. The daemon's own probe cycles feed the check."""
    from ttp import alerts
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    _ssh_255(p)   # the check that the named host is out of reach gets ssh's own 255 too
    stub = p.root / "reach.sh"
    stub.write_text("echo 'ssh: connect to host gw.example port 22: Connection timed out' >&2; exit 255\n")
    probe = f"sh {stub}"
    from ttp import netwait
    monkeypatch.setattr(netwait, "_ssh_config", lambda alias: {})
    d = Daemon(p.base)
    for i in range(2):
        assert coord.apply(p, [{"type": "task_add", "title": f"device job {i}", "spec": "s",
                                "start_when": probe}]) == []
    tids = [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE status='queued' ORDER BY id")]
    clock = {"now": time.time()}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    start = clock["now"]

    def cycle(at):
        clock["now"] = at
        d.probe_waiting()   # starts the probe once its last run is PROBE_EVERY_S old
        for proc, _, _ in list(d._probes.values()):
            proc.wait(timeout=30)
        d.probe_waiting()   # reads its verdict
        d._net_checked = 0.0
        d.check_unreachable()
        alerts.sweep(p.db, at)
        return p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='network:unreachable' ORDER BY id")

    step = dmod.PROBE_EVERY_S + 20
    at = start
    while at < start + 900:
        assert cycle(at) == [], "within the grace nothing is said"
        at += step
    sent = cycle(at)
    assert len(sent) == 1 and "gw.example" in sent[0]["text"] and "2 queued task(s)" in sent[0]["text"], sent
    assert all(p.db.task(t)["status"] == "queued" and deferral(p.db.task(t)).get("when") for t in tids)
    # The network is back: the probe passes and the tasks are free to start.
    stub.write_text("exit 0\n")
    at += step
    assert len(cycle(at)) == 1 and alerts.needs_you(p.db, at), "the alert holds through the debounce"
    assert all(not deferral(p.db.task(t)).get("when") for t in tids), "start_when passed"
    assert not d._start_rc, "a passed start_when leaves no verdict behind"
    for t in tids:
        p.db.update_task(t, status="running")
        p.db.x("INSERT INTO runs(task,role,started,status) VALUES(?,'worker',?,'running')", (t, at))
    at += 660
    assert len(cycle(at)) == 1, "clearing posts no new alert"
    assert not alerts.needs_you(p.db, at) and not p.db.kv("network_outage"), "the outage ended after the debounce"
    assert len(p.db.q("SELECT id FROM messages WHERE kind='resolved' AND ref='network:unreachable'")) == 1



@pytest.mark.parametrize("up", [True, False])
def test_a_probe_255_is_a_network_outage_only_when_its_host_does_not_answer(env, monkeypatch, up):
    """A start_when that exits 255 through its own `|| exit 255` after its remote command failed, while
    the host answers ssh, is a broken probe against its task, never a network outage. With the host
    really out of reach (ssh stubbed to 255) the outage is alerted, naming the alias."""
    from ttp import alerts, netwait
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    p = make(env)
    _no_events(p)
    _ssh_255(p, up=up)
    monkeypatch.setattr(netwait, "_ssh_config", lambda alias: {})
    probe = "m=$(ssh -o BatchMode=yes box-a stat /x) || exit 255"
    d = dmod.Daemon(p.base)
    assert coord.apply(p, [{"type": "task_add", "title": "device job", "spec": "s", "start_when": probe}]) == []
    tid = _added(p, "device job")
    clock = {"now": time.time()}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    at = start = clock["now"]
    while at < start + 1200:
        clock["now"] = at
        d.probe_waiting()
        for proc, _, _ in list(d._probes.values()):
            proc.wait(timeout=30)
        d.probe_waiting()
        d._net_checked = 0.0
        d.check_unreachable()
        alerts.sweep(p.db, at)
        at += dmod.PROBE_EVERY_S + 20
    sent = p.db.q("SELECT * FROM messages WHERE kind='alert' AND ref='network:unreachable'")
    broken = _events(p, tid, "deferral_probe_broken")
    if up:
        assert sent == [] and not p.db.kv("network_outage"), "the host answers: no network outage"
        assert len(broken) == 1 and f"#{tid}" in broken[0]["text"] and "ssh reaches box-a" in broken[0]["text"], broken
        assert "task %d probe is broken" % tid in (p.logs / "daemon.log").read_text()
    else:
        assert len(sent) == 1 and "box-a" in sent[0]["text"], sent
        assert p.db.kv("network_outage")["hosts"] and not broken
    assert p.db.task(tid)["status"] == "queued"



def test_failover_moves_off_a_logged_out_provider_only_to_a_usable_target(env):
    p, d, now = _failover_setup(env, util=10.0)
    tid = _codex_task(p)
    d.update_gates()
    d.failover_pinned()
    assert p.db.task(tid)["provider"] == "codex", "moved off a green provider"
    d.open_breaker("codex", "401")
    d.open_breaker("fake", "401")
    d._failover_checked = 0
    d.failover_pinned()
    assert p.db.task(tid)["provider"] == "codex", "moved to a logged-out target"
    from ttp import alerts
    p.db.set_kv(alerts.BREAKER + "fake", {})
    d._failover_checked = 0
    d.failover_pinned()
    t = p.db.task(tid)
    assert (t["provider"], t["model"], t["effort"]) == ("fake", None, "xhigh")
    assert "logged out" in t["spec"] and "no known end" in t["spec"]



def test_a_provider_limit_needs_the_user_only_when_the_account_is_out_of_funds(env, capsys):
    """A usage or rate limit lifts by itself: a low status line in the feed that clears itself, never
    "needs you", a chat alert or the desktop. An account out of credits only the user can fix: high,
    on top, even after a status line for the same provider."""
    p = make(env)
    from types import SimpleNamespace
    from ttp import alerts
    from ttp.cli import _listen_loop, status_text
    from ttp.daemon import Daemon
    from ttp.web import attention, fix_for, state_payload
    d = Daemon(p.base)
    now = time.time()
    p.db.set_kv("limited:fake", {"until": now + 3600, "note": "usage limit reached"})
    d.alert("limit:fake", "fake hit a limit: usage limit reached.", "low")
    assert attention(p.db, now) == [] and "needs you" not in status_text(p)
    feed = [m for m in state_payload(p, p.db)["feed"] if m["text"].startswith("fake hit a limit")]
    assert [m["state"] for m in feed] == ["status"], feed
    assert fix_for("fake", "usage limit reached") == "resumes by itself when the limit resets"
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (now, "t", now))
    _listen_loop(p, p.db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "low")
    assert "fake hit a limit" in capsys.readouterr().out, "a live status line reaches a chat that asked for low"
    # The same provider then refuses for funds: news at once, on top, though its status episode is open.
    d.alert("limit:fake", "fake refused work: out of credits. The account needs more credits.", "high")
    assert [m["text"] for m in attention(p.db, now)] == [
        "fake refused work: out of credits. The account needs more credits."]
    assert fix_for("fake", "out of credits") == "the account needs more credits or a higher spend limit"
    assert len(p.db.q("SELECT id FROM alerts WHERE key='limit:fake'")) == 1
    p.db.set_kv("limited:fake", {"until": now - 1, "note": "out of credits"})
    d.sweep_alerts()
    assert attention(p.db, time.time()) == []
    told = p.db.q("SELECT severity FROM messages WHERE kind='resolved' AND ref='limit:fake'")
    assert told == [{"severity": "normal"}], told   # a cleared high alert is told as before
    # A status line that cleared before a chat read it is not delivered late.
    p.db.set_kv("limited:other", {"until": time.time() - 1, "note": "rate limit"})
    p.db.post("out", "other hit a limit: rate limit.", chat=None, kind="alert", severity="low", ref="limit:other")
    _listen_loop(p, p.db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "low")
    assert "other hit a limit" not in capsys.readouterr().out
    assert alerts.cleared(p.db, {"kind": "alert", "severity": "low", "ref": "limit:other",
                                 "ts": time.time()}, time.time())



@pytest.mark.parametrize('caught_up', [False, '0.5.0', '0.5.1'])
def test_after_push_deploy_followup_after_takeover(env, tmp_path, caught_up):
    from ttp import alerts, deploy, overview, release
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    report = overview.deployed(names=['alpha'])
    report['projects'][0]['takeover_at'] = now + 1
    assert deploy.record(p, report, now)['rc'] == 0
    due = now + 1 + deploy.GRACE_S
    pages = []
    def page(key, text, severity, **_):
        pages.append((key, text, severity))
        p.db.post('out', text, kind='alert', ref=key, severity=severity)
    deploy.tick(p, page, due - 1)
    assert not pages
    if caught_up:
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt'].update(version=caught_up, commit=DEPLOY_C if caught_up == '0.5.0' else '123456789abcd')
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (due - 1, due - 1))
    deploy.tick(p, page, due + deploy.PROBE_S)
    deploy.tick(p, page, due + deploy.PROBE_S * 2)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    if caught_up:
        assert rec['closed'] and not pages
    else:
        assert len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha' and pages[0][2] == 'normal'
        assert 'missed takeover' in pages[0][1] and 'PDT' in pages[0][1]
        assert alerts.holds(p.db, pages[0][0], now, due)
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt']['version'] = '0.5.0'
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (due, due))
        assert [e['key'] for e in alerts.sweep(p.db, now=due)] == ['deploy-behind:alpha']
        assert not alerts.active(p.db, pages[0][0], now, due)



@pytest.mark.parametrize('caught_up', [True, False])
@pytest.mark.parametrize('merge_task', [False, True])
def test_after_push_deploy_restart_pending_is_owned_and_rechecked_after_its_wait(env, tmp_path, caught_up, merge_task):
    """Files at the release, daemon still on the old one: its own daemon restarts onto them, so it is
    pending (one low note) until delivery.deploy_restart_wait_s, then alerts once if still behind."""
    from ttp import alerts, deploy, overview, release
    p = make(env)
    p.set_config('home_timezone', 'America/Los_Angeles')
    p.set_config('delivery.deploy_restart_wait_s', 600)
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.5.0', running='0.4.9')
    if merge_task:   # a harness task still open from the merge: the restart is still the daemon's step
        db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    result = deploy.record(p, overview.deployed(names=['alpha']), now)
    assert result['rc'] == 0 and result['projects'][0]['outcome'] == 'pending'
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['owner'] == "this project's daemon" and rec['due'] == now + 600
    notes = p.db.q("SELECT severity,text FROM messages WHERE direction='out'")
    assert [n['severity'] for n in notes] == ['low'] and 'alpha: restart onto the new files pending' in notes[0]['text']
    assert not p.db.q('SELECT * FROM alerts')
    pages, page = _deploy_pager(p)
    deploy.tick(p, page, now + 599)
    assert not pages
    if caught_up:
        hb = base / 'state' / 'heartbeat'
        data = json.loads(hb.read_text())
        data['receipt']['version'] = '0.5.0'
        hb.write_text(json.dumps(data))
        os.utime(base / 'state' / 'tick_done', (now + 590, now + 590))
    for t in (now + 601, now + 601 + deploy.PROBE_S, now + 601 + deploy.PROBE_S * 2):
        deploy.tick(p, page, t)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    if caught_up:
        assert rec['closed'] and not pages
    else:
        assert len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha' and pages[0][2] == 'normal'
        assert 'still behind at its re-check' in pages[0][1] and 'PDT' in pages[0][1]
        assert alerts.holds(p.db, 'deploy-behind:alpha', now, now + 700)



@pytest.mark.parametrize('fault', ['stopped', 'upgrade_error'])
def test_after_push_deploy_fault_while_pending_alerts_at_next_probe(env, tmp_path, fault):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    assert deploy.record(p, overview.deployed(names=['alpha']), now)['rc'] == 0
    due = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']['due']
    pages, page = _deploy_pager(p)
    deploy.tick(p, page, now + 1)
    assert not pages
    if fault == 'stopped':
        db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    else:
        db.set_kv(release.KV_AUTO, {'key': f'0.5.0 {DEPLOY_C[:12]}', 'outcome': 'failed',
                                    'why': 'runtime import failed'})
    t = now + 1 + deploy.PROBE_S
    p = Project(p.base)                          # the reporting daemon restarted meanwhile
    pages, page = _deploy_pager(p, pages)
    deploy.tick(p, page, t)
    assert t < due and len(pages) == 1 and pages[0][0] == 'deploy-behind:alpha'
    assert ('daemon stopped' if fault == 'stopped' else 'runtime import failed') in pages[0][1]
    p = Project(p.base)
    pages, page = _deploy_pager(p, pages)
    for i in (2, 3):
        deploy.tick(p, page, now + 1 + deploy.PROBE_S * i)
    assert len(pages) == 1, 'one alert per condition, also across restarts'
    assert alerts.holds(p.db, 'deploy-behind:alpha', now, t)
    if fault == 'stopped':
        db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    else:
        db.set_kv(release.KV_AUTO, {})
    t = now + 1 + deploy.PROBE_S * 4
    deploy.tick(p, page, t)
    assert [e['key'] for e in alerts.sweep(p.db, now=t)] == ['deploy-behind:alpha']
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['due'] == due and rec['outcome'] == 'pending' and len(rec['history']) == 1
    deploy.tick(p, page, due + 1)
    assert len(pages) == 2 and 'missed takeover' in pages[1][1], 'the merge deadline is still watched'



def test_a_daemon_that_never_restarts_onto_new_releases_is_one_alert(env, tmp_path):
    """Files reach each release but the daemon keeps running the old code: each release set a fresh restart
    deadline, the failed check was superseded, its alert cleared and a new one opened after the wait. The
    earlier restart deadline is kept, so it is one alert, updated in place, never cleared in between."""
    from ttp import alerts, deploy, overview
    p = make(env)
    base, _ = _deploy_project(tmp_path, 'alpha', '0.5.0', running='0.4.9')
    pages, page = _deploy_pager(p)
    now = time.time()
    versions = [f'0.5.{i}' for i in range(5)]
    for i, v in enumerate(versions):
        t = now + 3600 * i
        (base / 'harness' / 'runtime' / 'ttp' / '__init__.py').write_text(f'__version__ = "{v}"\n')
        _deploy_lib(v)
        deploy.record(p, overview.deployed(names=['alpha']), t)
        alerts.sweep(p.db, now=t)
        deploy.tick(p, page, t + deploy.RESTART_WAIT_S + 400)
        alerts.sweep(p.db, now=t + deploy.RESTART_WAIT_S + 400)
    rows = p.db.q("SELECT key,text,cleared FROM alerts WHERE key='deploy-behind:alpha'")
    assert len(rows) == 1 and rows[0]['cleared'] is None, rows
    assert f'tt-project {versions[-1]} ' in rows[0]['text']
    assert not p.db.q("SELECT text FROM messages WHERE text LIKE '%Cleared%'")
    assert len(pages) == 1



def test_after_push_deploy_new_commit_of_same_version_retargets_the_check(env, tmp_path):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    base, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    assert deploy.record(p, overview.deployed('0.5.0', DEPLOY_C, names=['alpha']), now)['rc'] == 0
    pages, page = _deploy_pager(p)
    db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    deploy.tick(p, page, now + 1)
    assert len(pages) == 1
    db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    newer = '123456789abcdef01234'
    again = Project(p.base)
    assert deploy.record(again, overview.deployed('0.5.0', newer, names=['alpha']), now + 10)['rc'] == 0
    rec = again.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['commit'] == newer and rec['outcome'] == 'pending' and len(rec['history']) == 1
    assert not alerts.holds(p.db, 'deploy:alpha:0.5.0', now, now + 10)
    deploy.record(again, overview.deployed('0.5.0', newer, names=['alpha']), now + 20)
    assert again.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0'] == rec, 'a true retry keeps its deadline'
    _deploy_catch_up(base, db, '0.5.0', newer, now + 30)
    deploy.tick(Project(p.base), page, now + 30)
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['closed'] and len(pages) == 1
    assert not alerts.holds(p.db, 'deploy:alpha:0.5.0', now, rec['due'] + 1)
    deploy.tick(p, page, rec['due'] + 1)
    assert len(pages) == 1, 'no missed-takeover alert for a release that arrived'



def test_after_push_deploy_recovered_daemon_returns_to_owned_pending(env, tmp_path):
    from ttp import alerts, deploy, overview, release
    from ttp.project import Project
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    db.set_kv('daemon', {'pid': 0, 'host': 'testhost'})
    now = time.time()
    assert deploy.record(p, overview.deployed(names=['alpha']), now)['rc'] == 1
    due = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']['due']
    assert due > now + 3600
    db.set_kv('daemon', {'pid': os.getpid(), 'host': 'testhost'})
    again = Project(p.base)
    for t in (now + 60, now + 120):         # the coordinator's retry, after a restart
        result = deploy.record(again, overview.deployed(names=['alpha']), t)
        assert result['rc'] == 0 and result['projects'][0]['outcome'] == 'pending'
    assert len(p.db.q("SELECT * FROM messages WHERE kind='alert'")) == 1
    assert [e['key'] for e in alerts.sweep(p.db, now=now + 120)] == ['deploy-behind:alpha']
    rec = p.db.kv(deploy.CHECKS)['deploy:alpha:0.5.0']
    assert rec['due'] == due and len(rec['history']) == 1 and not rec.get('alerted')
    pages, page = _deploy_pager(again)
    deploy.tick(again, page, now + 180)
    assert not pages
    deploy.tick(again, page, due + 1)
    assert len(pages) == 1 and 'missed takeover' in pages[0][1]



def test_failover_review_lines_say_when_a_logout_ended(env):
    """A logged-out cause whose breaker closed since shows its end and the task's status; an open one keeps 'no known end'."""
    from ttp import alerts, failover
    p = make(env)
    now = time.time()
    tid = p.db.add_task("long job", "Run it.", kind="work", tier="standard", provider="claude")
    line = (f"#{tid} long job: left running on claude, which cannot start new work (logged out) with no known end; "
            "a later run of it moves unless its pin is required")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (now - 5 * 3600, "daemon", failover.KIND, "low", line, "handled", tid))
    p.db.set_kv(alerts.BREAKER + "claude", {"open": True, "opened": now - 6 * 3600})
    assert failover.review_lines(p.db, now, where="America/Los_Angeles") == [line]
    p.db.set_kv(alerts.BREAKER + "claude", {"open": False, "opened": now - 6 * 3600, "closed": now - 3.5 * 3600})
    p.db.update_task(tid, status="done")
    got = failover.review_lines(p.db, now, where="America/Los_Angeles")[0]
    assert "no known end" not in got and "(ended " in got and "lasted 2.5 h)" in got, got
    assert got.endswith(f"#{tid} is now done") and (" PDT, " in got or " PST, " in got), got


def test_headline_label_prefix_and_word_clip_in_chain_audit():
    from ttp import headline
    assert headline.restates("triage: box-a hold", "box-a hold now cleared", False)
    assert headline.join("triage: box-a hold", "box-a hold", 80) == "box-a hold"
    out = headline.clip("alpha beta gamma delta " * 5, 50)
    assert out.endswith("…") and len(out) <= 50 and out[:-1].split()[-1] in {"alpha", "beta", "gamma", "delta"}


def test_a_worker_high_notify_reaches_the_coordinator_while_its_run_lives(env, monkeypatch, capsys):
    """`ttp notify --severity high`: synced to the run's escalation file, which the daemon reads each
    tick while the run lives. Each distinct text becomes one high coordinator observation, never a
    user alert; repeats and rereads add nothing, past ESCALATIONS_PER_RUN texts are counted in the
    hand-off, and a run killed before any tick still delivers its lines when it is finished."""
    from ttp import cli, daemon
    from ttp.daemon import Daemon
    from ttp.db import load_result
    from ttp.providers.base import RunUsage as Usage

    def notify(*args) -> int:
        try:
            cli.main(["notify", *args])
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    def escalations(tid: int) -> list:
        return p.db.q("SELECT text, severity, status, kind FROM events WHERE task=? AND fingerprint LIKE 'escalate:%' "
                      "ORDER BY id", (tid,))
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("probe", "spec", kind="work", tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))
    rid = p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker','claude','running',?,?)",
                 (tid, time.time(), str(run_dir)))
    assert notify("--severity", "critical", "x") == 2, "critical is refused"
    assert not (run_dir / daemon.ESCALATE_FILE).exists()
    assert notify("--severity", "high", "device lost mid-run:\n  results at risk") == 0
    assert "coordinator" in capsys.readouterr().out
    assert "escalate: device lost" in (run_dir / "progress.md").read_text()
    assert not (run_dir / daemon.NOTIFY_FILE).exists(), "high never takes the user's notify path"
    d.relay_escalations()
    got = escalations(tid)
    assert len(got) == 1 and got[0]["severity"] == "high" and got[0]["status"] == "queued", got
    assert got[0]["kind"] == "observation" and "device lost mid-run: results at risk" in got[0]["text"]
    # The same text again, an unchanged file, and a reread from scratch (a daemon restart) add nothing.
    assert notify("--severity", "high", "device lost mid-run: results at risk") == 0
    d.relay_escalations()
    d.relay_escalations()
    d._escalated.clear()
    d.relay_escalations()
    assert len(escalations(tid)) == 1
    # The cap: two distinct texts per run; the rest are counted, never queued.
    for i in range(3):
        assert notify("--severity", "high", f"another finding {i}") == 0
    d.relay_escalations()
    assert len(escalations(tid)) == daemon.ESCALATIONS_PER_RUN == 2
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert'"), "the user is never paged directly"
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "probed"}))
    d._finish_worker(dict(p.db.one("SELECT * FROM runs WHERE id=?", (rid,))), Usage(cost_usd=0.1), "ok", run_dir)
    assert len(escalations(tid)) == 2
    assert load_result(p.db.task(tid)["result"])["escalations_dropped"] == 2

    # A run killed before the daemon ever read its file: its run end (here after a reboot) delivers it.
    t2 = p.db.add_task("probe two", "spec", kind="work", tier="standard", origin="coordinator")
    p.db.update_task(t2, status="running")
    run2 = _note_run(env, monkeypatch, p, task=str(t2))
    r2 = p.db.x("INSERT INTO runs(task,role,provider,status,started,dir) VALUES(?,'worker','claude','running',?,?)",
                (t2, time.time(), str(run2)))
    assert notify("--severity", "high", "found corrupt output") == 0
    (run2 / "result.json").write_text(json.dumps({"status": "failed", "summary": "cut short"}))
    d._finish_worker(dict(p.db.one("SELECT * FROM runs WHERE id=?", (r2,))), Usage(cost_usd=0.1), "ok", run2,
                     rebooted=True)
    assert [e["text"].endswith("found corrupt output") for e in escalations(t2)] == [True]
    assert "escalations_dropped" not in load_result(p.db.task(t2)["result"])


def test_worker_low_and_normal_notify_are_unchanged_by_the_escalation_path(env, monkeypatch, capsys):
    """Low and normal stay user alerts posted when the run ends; no escalation file, no event."""
    from ttp import cli, daemon
    p = make(env)
    tid = p.db.add_task("probe", "spec", kind="work", tier="standard", origin="coordinator")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))
    for sev in ("low", "normal"):
        cli.main(["notify", "--severity", sev, f"{sev} news"])
    assert "sent when this run ends" in capsys.readouterr().out
    assert not (run_dir / daemon.ESCALATE_FILE).exists()
    assert daemon.relay_worker_escalations(p, p.db.task(tid), 9, run_dir) == (0, 0)
    assert daemon.relay_worker_notifies(p, p.db.task(tid), run_dir) == 2
    assert not p.db.one("SELECT id FROM events WHERE fingerprint LIKE 'escalate:%'")


def _blocked_on(env, p, question, *, target_status="queued", extra="", target=None):
    """A running task hands off `blocked` with `question`; a second task (`target`, made in
    `target_status` unless given) is the one it names as {t}; {me} is the task itself."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    if not (p.config().get("delivery") or {}).get("push_branch"):
        p.set_config("delivery.push_branch", "origin/proj")
    if target is None:
        target = p.db.add_task("the change it needs", "s", origin="coordinator")
        p.db.update_task(target, status=target_status)
    tid = p.db.add_task("needs it", "s", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "blocked", "summary": "stuck", "question": question.format(t=target, u=target + 50, me=tid)}))
    Daemon(p.base)._finish_worker({"task": tid, "note": None}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid, target


def test_a_hand_off_blocked_only_on_one_task_landing_becomes_a_landed_wait(env):
    p = make(env)
    tid, target = _blocked_on(env, p, "Re-queue me after #{t} lands on the push branch")
    row = p.db.task(tid)
    result = json.loads(row["result"])
    assert row["status"] == "queued" and result["status"] == "waiting"
    assert result["retry_when"] == f"landed:#{target}" and "Re-queue me after" in result["summary"]
    ev = p.db.one("SELECT * FROM events WHERE kind='task_wait_landed' AND task=?", (tid,))
    assert ev["severity"] == "low" and ev["status"] == "handled"
    assert not p.db.q("SELECT * FROM events WHERE task=? AND kind='task_blocked'", (tid,))


def test_a_branch_name_counts_as_the_named_task(env):
    p = make(env)
    tid, target = _blocked_on(env, p, "waiting for ttp/t{t}-fix to be merged")
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == f"landed:#{target}"


@pytest.mark.parametrize("question", ["Is #{t} really landed, or #{u}?", "Need a decision about #{t}",
                                      "Need credentials for the box", "Is the thing landed?",
                                      "Need ssh credentials for the box; also #{t} must land first",
                                      "Blocked until the user merges PR #{t}",
                                      "Ask #{t} must be answered before this can be pushed",
                                      "Should I wait for #{t} to land or revert it?",
                                      "owner/repo#{t} must be merged upstream",
                                      "Which approach do you prefer? Also #{t} must land first.",
                                      "Can you re-queue me once #{t} lands?",
                                      "Re-queue me after #{t} lands, then #{u} too",
                                      "Re-queue me after #{me} lands",
                                      "Re-queue me after #{u} lands"])
def test_a_blocked_hand_off_that_is_not_one_task_landing_stays_blocked(env, question):
    p = make(env)
    tid, _ = _blocked_on(env, p, question)
    assert p.db.task(tid)["status"] == "blocked"
    assert json.loads(p.db.task(tid)["result"])["status"] == "blocked"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_blocked_hand_off_on_a_failed_or_cancelled_task_stays_blocked(env, state):
    p = make(env)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target_status=state)
    assert p.db.task(tid)["status"] == "blocked"


def _no_network(monkeypatch):
    """Every git fetch, ls-remote, pull or push started while handling the result, recorded."""
    calls, real = [], subprocess.run

    def run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and {"fetch", "ls-remote", "pull", "push"} & {str(c) for c in cmd}:
            calls.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_a_blocked_hand_off_on_an_already_landed_task_stays_blocked(env, monkeypatch):
    """Its landing is on the local push-branch ref: a wait would pass at once, so no wait is made."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    target, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    _git_out(path, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    calls = _no_network(monkeypatch)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target=target)
    assert p.db.task(tid)["status"] == "blocked" and not calls, calls


def test_a_blocked_hand_off_stays_blocked_when_the_push_branch_is_unknown(env, monkeypatch):
    """No push branch: its `landed:#<id>` probe could never pass, so no wait is made."""
    from ttp import landed
    monkeypatch.setattr(landed, "_push_ref", lambda p, repo: (_ for _ in ()).throw(ValueError("no target")))
    p = make(env)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands")
    assert p.db.task(tid)["status"] == "blocked"


def test_the_landed_check_on_a_blocked_hand_off_reads_local_refs_only(env, monkeypatch):
    """A done target not on the local push-branch ref converts with no fetch: the wait's probe fetches."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    target, path, branch = _code_task(p, "feature")
    _commit(path, "feature.txt", "feature\n")
    calls = _no_network(monkeypatch)
    tid, _ = _blocked_on(env, p, "Re-queue me after #{t} lands", target=target)
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == f"landed:#{target}"
    assert not calls, calls


def test_outage_facts_state_the_start_duration_actor_task_and_what_was_tried(env):
    """outagemsg.facts: one wording for every outage message, for the owner acting, the owner paused
    with the fallback holding a tracked task, and nobody acting."""
    from ttp import outagemsg
    t0 = 1_790_000_000.0
    owner = outagemsg.facts(t0, t0 + 4 * 3600, outagemsg.actor("alpha", "owner"), "UTC",
                            tried=["recovery page 1 to alpha"], next_step="alpha recovers it")
    assert owner.startswith(f"Since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(t0))} UTC (4.0 h so far).")
    assert "Acting now: alpha (owner)." in owner and "Tracked task: none." in owner
    assert "Tried: recovery page 1 to alpha." in owner and owner.endswith("Next: alpha recovers it.")
    fallback = outagemsg.facts(t0, t0 + 1500, outagemsg.actor("beta", "fallback", "alpha cannot act: it is paused"),
                               "UTC", task="#42", due=t0 + 3 * 3600)
    assert "(25 min so far)" in fallback and "Acting now: beta (fallback; alpha cannot act: it is paused)." in fallback
    assert f"Tracked task: #42, due {time.strftime('%H:%M', time.gmtime(t0 + 3 * 3600))} UTC." in fallback
    assert "Tried: nothing yet." in fallback
    for acting in (outagemsg.actor(None), outagemsg.actor("the user"), ""):
        nobody = outagemsg.facts(t0, t0 + 2 * 86400, acting, "UTC", task="not a task", due="soon")
        assert "(2.0 d so far)" in nobody and "Acting now: nobody: escalating" in nobody, nobody
        assert "Tracked task: none." in nobody, "a task id or due time that is not one is left out"


def test_a_bare_nothing_was_changed_outage_message_never_goes_out_without_an_actor(env):
    from ttp import outagemsg
    from ttp.daemon import Daemon
    bare = ("Device held for 4 h on box-a. Nothing was changed: the owner is paused and the fallback "
            "was sent a note.")
    assert outagemsg.bare(bare) and outagemsg.bare("box-a down; sent the fallback a note.")
    assert not outagemsg.bare(bare + " Acting now: beta (fallback).")
    assert outagemsg.ensure_actor(bare).endswith("Acting now: nobody: escalating to the user.")
    p = make(env)
    d = Daemon(p.base)
    for key in ("outage:box-a", "machine:box-a:held", "heal:dev", "heal-hold:dev", "network:unreachable"):
        d.alert(key, bare, "high")
        got = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (key,))["text"]
        assert got.startswith(bare) and "Acting now: nobody: escalating" in got, (key, got)
    d.alert("box-a-gone", bare, "high", outage=True)
    assert "Acting now:" in p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='box-a-gone'")["text"]


def test_an_alert_off_the_outage_paths_is_never_tagged_with_an_actor_by_its_words(env):
    """Only outage paths opt in (a key prefix or outage=True): 'held', 'down', 'owner', 'recovery' in a
    task title or a push message do not make an alert about an outage."""
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    texts = {
        "root-checkout:7": ("The project root's checkout (/repo) was left with uncommitted changes to 1 tracked "
                            "path it did not have before: a.py after run 3 of task #7 (Fix held device recovery "
                            "when the owner is down), which worked in the project root. Nothing was changed; this "
                            "clears once the checkout is back and those paths are clean."),
        "push-held": "Push held: nothing was changed.",
        "hold-stale:3": "Run 3 has held box-a for 5.0 h; nothing was changed, as a job may still use it.",
    }
    for key, text in texts.items():
        d.alert(key, text, "low", every_s=0)
        got = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (key,))["text"]
        assert got == text, (key, got)
    d.alert("outage:box-b", texts["push-held"], "high", outage=False)
    assert p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='outage:box-b'")["text"] == texts["push-held"]


def test_machine_pages_and_status_say_how_long_and_who_acts_now(env):
    """A machine condition's page names its start, duration so far and the actor at each step: the owner,
    the fallback once the owner cannot act (naming the outage task queued in it), then nobody."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import responsibilities
    from ttp.daemon import Daemon
    alpha = make(env, "alpha")
    mm.add("box-a", tags="device")
    mm.set_recovery("box-a", owner="alpha", fallback="beta")
    t0 = time.time() - 6 * 3600
    ml.report("gamma", "box-a", "held", "device held, no holder", now=t0)
    able = {"alpha": (False, "it is paused"), "beta": (True, "")}
    first = t0 + ml.GRACE_S

    def recovery(at):   # every page goes out; the recovery pages are returned
        got = ml.route(at, able=able.get, healthy=lambda *a: [])
        for x in got:
            ml.delivered(x)
        return [x for x in got if x["kind"] == "machine_condition"]
    pages = recovery(first)
    assert [x["target"] for x in pages] == ["alpha"]
    text = pages[0]["text"]
    assert "Machine box-a held" in text and "(30 min so far)" in text and "Acting now: alpha (owner)." in text, text
    assert "Tried: nothing yet." in text and "Tracked task: none." in text and "Next: alpha checks" in text, text
    # The owner cannot act: the outage task (outagetask.py) is queued in the fallback; later pages name it.
    ml.report("gamma", "box-a", "held", "device held, no holder", now=first + 60)
    recovery(first + 60)
    later = first + 60 + ml.UNAVAILABLE_S
    pages = recovery(later)
    assert [x["target"] for x in pages] == ["beta"]
    text = pages[0]["text"]
    assert "Acting now: beta (fallback; alpha cannot act: it is paused)." in text, text
    assert f"Tracked task: outage:box-a:held@{int(t0)} in beta, due " in text, text
    assert "Tried: recovery page 1 to alpha." in text and "(1.0 h so far)" in text, text
    line = responsibilities.machine_lines(next(r for r in responsibilities.machine_status("box-a")
                                               if r["alias"] == "box-a"))
    cond = next(x for x in line if x.startswith("  condition: "))
    assert "so far; seen by gamma" in cond and "Acting now: beta (fallback)" in cond, cond
    assert f"Tracked task: outage:box-a:held@{int(t0)} in beta" in cond, cond
    able["beta"] = (False, "its daemon is stopped")
    ml.report("gamma", "box-a", "held", now=later + 60)
    recovery(later + 60)
    pages = recovery(later + 60 + ml.UNAVAILABLE_S)
    assert [x["target"] for x in pages] == [ml.USER]
    text = pages[0]["text"]
    assert "Acting now: nobody: escalating to the user (beta cannot act: its daemon is stopped)." in text, text
    assert "recovery page 1 to alpha; recovery page 2 to beta" in text, text
    Daemon(alpha.base)._machine_page(pages[0], later)
    got = alpha.db.one("SELECT text FROM messages WHERE kind='alert' AND ref=?", (pages[0]["key"],))["text"]
    assert got.startswith("Outage: Machine box-a held") and got.count("Acting now:") == 1, got
    # A page recorded before this wording still names the actor.
    Daemon(alpha.base)._machine_page({**pages[0], "key": "machine:old", "text": "Machine box-a held."}, later)
    old = alpha.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='machine:old'")["text"]
    assert old == "Outage: Machine box-a held. Acting now: nobody: escalating to the user.", old

def _due(p, name):
    p.db.x("UPDATE schedules SET next_run=? WHERE name=?", (time.time() - 1, name))


def _settle(d, name, within=20.0):
    """Tick the schedules until `name`'s run is recorded; its row."""
    end = time.monotonic() + within
    d.run_schedules()
    while name in d._sched_runs and time.monotonic() < end:
        d.run_schedules()
    assert name not in d._sched_runs, f"schedule {name} still running after {within} s"
    return d.p.db.one("SELECT * FROM schedules WHERE name=?", (name,))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = f"/proc/{pid}/stat"
    return not os.path.exists(stat) or open(stat).read().rsplit(") ", 1)[1][0] != "Z"


def test_a_slow_command_schedule_never_holds_the_tick(env, monkeypatch):
    """A command that runs far longer than a tick goes on off the tick: the tick's later steps (message
    handling, dispatch) still run within seconds, and its result is recorded on a later tick."""
    from ttp import daemon as dm, schedule as sched
    p = make(env)
    sched.upsert(p.db, "slow", "command", "1h", payload={"command": "sleep 4; echo '{\"text\": \"done\"}'",
                                                        "timeout_s": 60})
    _due(p, "slow")
    d = dm.Daemon(p.base)
    later = []
    monkeypatch.setattr(d, "maybe_coordinate", lambda: later.append("coordinate"))
    monkeypatch.setattr(d, "dispatch", lambda: later.append("dispatch"))
    t0 = time.monotonic()
    d.tick()
    assert time.monotonic() - t0 < dm.SCHEDULE_WAIT_S + 3, "the tick waited for the slow schedule"
    assert later == ["coordinate", "dispatch"] and "slow" in d._sched_runs
    assert p.db.one("SELECT last_run FROM schedules WHERE name='slow'")["last_run"] is None
    row = _settle(d, "slow")
    assert row["last_status"] == "ok (1 observations)" and row["last_run"]


def test_a_command_schedule_past_its_timeout_has_its_group_killed_and_counts_as_failed(env, tmp_path):
    """Its whole process group is killed (the shell's background child too) and the run is a failed
    one: an error observation, and two in a row queue the usual fix task."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    pidfile = tmp_path / "child.pid"
    sched.upsert(p.db, "hangs", "command", "1h", payload={
        "command": f"sleep 60 & echo $! > {pidfile}; wait", "timeout_s": 1})
    d = dm.Daemon(p.base)
    for n in (1, 2):
        _due(p, "hangs")
        row = _settle(d, "hangs")
        assert row["last_status"] == "error: timed out after 1 s", row["last_status"]
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "the command's background child outlived its timeout"
    assert p.db.q("SELECT id FROM issues WHERE source=? AND status='open'", (scr.error_source("watcher:hangs"),))
    assert "hangs" in (p.db.kv(d.SCHEDULE_FIXES_KEY) or {}), "two failed runs in a row queue the fix task"
    assert not p.db.kv(schedrun.GROUPS_KEY), "an ended group is no longer kept"


def test_a_schedule_still_running_at_its_next_due_time_is_not_started_twice(env, tmp_path):
    from ttp import daemon as dm, schedule as sched
    p = make(env)
    count = tmp_path / "starts"
    sched.upsert(p.db, "busy", "command", "1m", payload={"command": f"echo x >> {count}; test $(wc -l < {count}) -gt 1 || sleep 5",
                                                        "timeout_s": 60})
    d = dm.Daemon(p.base)
    for _ in range(2):   # 4 s at most: the first run takes 5
        _due(p, "busy")
        d.run_schedules()
    assert count.read_text().count("x") == 1 and "busy" in d._sched_runs
    _settle(d, "busy")
    _due(p, "busy")
    _settle(d, "busy")
    assert count.read_text().count("x") == 2, "once it ended it runs again when due"


def test_replay_a_hanging_gh_in_the_pr_watcher_does_not_stall_the_tick(env, monkeypatch):
    """Replay of a tick stalled in run_schedules: the builtin prs watcher sat in a gh call (subprocess
    communicate) that did not answer. The tick now goes on; a run past its timeout is recorded failed
    and its late result dropped, and no second run starts while the hung one still goes."""
    from ttp import daemon as dm, schedule as sched, watchers
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES(?,?,?,?,?)",
           ("t", "code", "done", "https://github.com/acme/widgets/pull/1", time.time()))
    gate, calls = threading.Event(), []

    def hung_gh(args, cwd):
        calls.append(args[0])
        gate.wait(30)
        return None
    monkeypatch.setattr(watchers, "_gh", hung_gh)
    monkeypatch.setattr(dm, "SCHEDULE_GRACE_S", 0)
    sched.upsert(p.db, "prs", "watcher", "5m", payload={"builtin": "prs", "deadline_s": 1})
    _due(p, "prs")
    d = dm.Daemon(p.base)
    later = []
    monkeypatch.setattr(d, "dispatch", lambda: later.append("dispatch"))
    t0 = time.monotonic()
    d.tick()
    assert time.monotonic() - t0 < dm.SCHEDULE_WAIT_S + 3 and later == ["dispatch"]
    time.sleep(1.1)
    d.run_schedules()
    row = p.db.one("SELECT * FROM schedules WHERE name='prs'")
    assert row["last_status"] == "error: timed out after 1 s", row["last_status"]
    _due(p, "prs")
    d.run_schedules()
    assert len(calls) == 1, "the hung run is not started again while it still goes"
    gate.set()
    _settle(d, "prs")
    assert p.db.one("SELECT last_status FROM schedules WHERE name='prs'")["last_status"].startswith("error: timed out")


def test_a_command_schedule_killed_by_a_daemon_stop_records_nothing_and_stays_due(env):
    """The stop kills the run's group; the command then ends -9, which is not its result: no error
    issue, no event to wake the coordinator, no last_run. The next daemon runs it again."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    sched.upsert(p.db, "long", "command", "1h", payload={"command": "sleep 60", "timeout_s": 120})
    _due(p, "long")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["long"]
    assert _wait_for(lambda: p.db.kv(schedrun.GROUPS_KEY), 10)
    events = p.db.one("SELECT COUNT(*) n FROM events")["n"]
    assert schedrun.stop(p.db, list(d._sched_runs.values())), "the stop killed its group"
    assert run.done.wait(5)
    time.sleep(1)
    d.run_schedules()
    assert "long" not in d._sched_runs
    assert not p.db.q("SELECT id FROM issues WHERE source=?", (scr.error_source("watcher:long"),))
    assert p.db.one("SELECT COUNT(*) n FROM events")["n"] == events, "the kill queued an event"
    row = p.db.one("SELECT last_run, next_run FROM schedules WHERE name='long'")
    assert row["last_run"] is None and row["next_run"] <= time.time(), "it stays due"


def test_a_heal_fix_killed_by_a_daemon_stop_does_not_escalate_and_rechecks_next_start(env):
    from ttp import daemon as dm, heal, schedrun, schedule as sched
    p = make(env)
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": {
        "check": "false", "fix": "sleep 60", "grace_s": 0, "settle_s": 600}})
    _due(p, "svc")
    d1 = dm.Daemon(p.base)
    d1.run_schedules()
    run = d1._sched_runs["svc"]
    assert _wait_for(lambda: heal.state(p.db, "svc").get("phase") == "fixing" and p.db.kv(schedrun.GROUPS_KEY), 10)
    assert schedrun.stop(p.db, list(d1._sched_runs.values()))
    assert run.done.wait(5)
    d1.run_schedules()
    st = heal.state(p.db, "svc")
    assert st.get("phase") == "fixing" and "fix_rc" not in st and not st.get("task"), st
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'"), "the killed fix escalated"
    assert p.db.one("SELECT last_run FROM schedules WHERE name='svc'")["last_run"] is None
    row = _settle(dm.Daemon(p.base), "svc")
    assert row["last_status"] == "unhealthy (fix interrupted; rechecking)", row["last_status"]
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'")


def test_a_run_killed_at_the_ticks_backstop_is_recorded_once_as_timed_out(env):
    """The tick's backstop (past the run's own bounds) records the timeout and kills the group; the
    command then ends -9 in its thread, which is not a second failure to record."""
    from ttp import daemon as dm, schedrun, schedule as sched, screen as scr
    p = make(env)
    sched.upsert(p.db, "long", "command", "1h", payload={"command": "sleep 60", "timeout_s": 120})
    _due(p, "long")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["long"]
    assert _wait_for(lambda: p.db.kv(schedrun.GROUPS_KEY), 10)
    run.deadline = time.monotonic()
    d.run_schedules()
    assert run.done.wait(5)
    time.sleep(0.5)
    d.run_schedules()
    assert "long" not in d._sched_runs
    titles = [r["title"] for r in p.db.q("SELECT title FROM issues WHERE source=?", (scr.error_source("watcher:long"),))]
    assert titles and not any("rc=-9" in t for t in titles), titles
    assert p.db.one("SELECT last_status FROM schedules WHERE name='long'")["last_status"].startswith("error: timed out")


def test_a_heal_fix_killed_at_the_ticks_backstop_does_not_escalate_its_kill(env):
    from ttp import daemon as dm, heal, schedrun, schedule as sched
    p = make(env)
    sched.upsert(p.db, "svc", "command", "5m", payload={"heal": {
        "check": "false", "fix": "sleep 60", "grace_s": 0, "settle_s": 600}})
    _due(p, "svc")
    d = dm.Daemon(p.base)
    d.run_schedules()
    run = d._sched_runs["svc"]
    assert _wait_for(lambda: heal.state(p.db, "svc").get("phase") == "fixing" and p.db.kv(schedrun.GROUPS_KEY), 10)
    run.deadline = time.monotonic()
    d.run_schedules()
    assert run.done.wait(5)
    d.run_schedules()
    st = heal.state(p.db, "svc")
    assert "fix_rc" not in st and not st.get("task"), st
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Self-fix%'"), "the backstop's kill escalated"
    assert p.db.one("SELECT last_status FROM schedules WHERE name='svc'")["last_status"].startswith("error: timed out")


def test_a_restarted_daemon_kills_the_groups_its_predecessors_runs_left_and_runs_them_once_more(env, tmp_path):
    from ttp import daemon as dm, schedrun, schedule as sched
    p = make(env)
    first = tmp_path / "first"
    sched.upsert(p.db, "long", "command", "1h", payload={
        "command": f"test -f {first} || {{ touch {first}; exec sleep 60; }}", "timeout_s": 120})
    _due(p, "long")
    d1 = dm.Daemon(p.base)
    d1.run_schedules()
    groups = p.db.kv(schedrun.GROUPS_KEY) or {}
    assert len(groups) == 1 and "long" in d1._sched_runs
    pid = int(next(iter(groups)))
    assert p.db.one("SELECT last_run FROM schedules WHERE name='long'")["last_run"] is None
    d2 = dm.Daemon(p.base)   # d1 is gone: its tick never records that run
    row = _settle(d2, "long")
    assert not _alive(pid), "the earlier daemon's command was killed"
    assert row["last_status"] == "ok (0 observations)", "the schedule ran once more, recorded once"
