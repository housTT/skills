# Suite cost watch tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new suite-cost tests here, not there.
from __future__ import annotations


def _suite_out(cpu, tests=100):
    return f"{tests} passed in 3.0s\nttp-suite: wall 10.0s cpu {cpu}s workers 4\n"


def _suite_events(db):
    return db.q("SELECT * FROM events WHERE kind='suite_cost_regression'")


def test_suite_cost_parse_and_missing_line(env):
    from ttp import suitecost
    assert suitecost.parse(_suite_out(50)) == {"wall": 10.0, "cpu": 50.0, "tests": 100}
    assert suitecost.parse("100 passed in 3s\n") is None
    assert suitecost.parse("ttp-suite: wall 1.0s cpu 2.0s workers 2\n") is None
    p = make(env)
    assert suitecost.record(p.db, "b1", "abc", "1.0.0", "100 passed\n") == ""
    assert not p.db.kv(suitecost.SAMPLES_KEY) and suitecost.audit_line(p.db) == ""


def test_suite_cost_threshold_dedupe_and_rearm(env):
    from ttp import suitecost
    p = make(env)
    t = 1_000_000.0
    for i in range(5):   # five samples: no judgement yet, even for a huge one
        assert suitecost.record(p.db, f"b{i}", "s", "1.0.0", _suite_out(50), t + i) == ""
    assert suitecost.record(p.db, "b5", "s", "1.0.1", _suite_out(70), t + 5) == ""   # 1.4x: under 1.5x
    text = suitecost.record(p.db, "b6", "s6", "1.0.2", _suite_out(100), t + 6)       # 2x
    assert "batch b6" in text and "1.0.2" in text and "first batch over 1.5x: b6" in text
    assert len(_suite_events(p.db)) == 1 and _suite_events(p.db)[0]["severity"] == "low"
    assert suitecost.record(p.db, "b7", "s7", "1.0.3", _suite_out(110), t + 7) == ""   # same episode
    assert len(_suite_events(p.db)) == 1
    # back under 1.2x of the median re-arms; the next regression raises again, naming its own first batch
    for i in range(8, 20):
        suitecost.record(p.db, f"b{i}", "s", "1.0.4", _suite_out(50), t + i)
    assert p.db.kv(suitecost.STATE_KEY) == {}
    assert "first batch over 1.5x: b20" in suitecost.record(p.db, "b20", "s", "1.0.5", _suite_out(500), t + 20)
    assert len(_suite_events(p.db)) == 2
    line = suitecost.audit_line(p.db, t + 21)
    assert "cpu 500s" in line and "7-day median" in line and "ms cpu/test" in line
