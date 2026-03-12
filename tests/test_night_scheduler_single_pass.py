import os
import sys
from datetime import datetime

sys.path.insert(0, os.getcwd())

import night_scheduler as ns


def test_run_nightly_maintenance_uses_single_night_job_and_updates_state(monkeypatch, tmp_path):
    marker = tmp_path / "night_research_day.txt"
    monkeypatch.setattr(ns, "RUN_MARKER", str(marker))

    calls = {"night_job": 0, "apply": 0, "save": 0}

    def fake_night_job():
        calls["night_job"] += 1
        return {
            "selected": ["RELIANCE", "TCS"],
            "details": {"scored": 2},
        }

    def fake_apply(universe, details=None):
        calls["apply"] += 1
        assert universe == ["RELIANCE", "TCS"]
        assert details == {"scored": 2}
        return {}

    def fake_save(universe, path):
        calls["save"] += 1
        assert universe == ["RELIANCE", "TCS"]
        return str(tmp_path / "universe_trading.txt")

    monkeypatch.setattr(ns.night_research, "run_night_job", fake_night_job)
    monkeypatch.setattr(ns.research_engine, "apply_night_universe", fake_apply)
    monkeypatch.setattr(ns, "save_universe", fake_save)

    state = {}
    ns.run_nightly_maintenance(state)

    assert calls == {"night_job": 1, "apply": 1, "save": 1}
    assert state.get("research_universe") == ["RELIANCE", "TCS"]
    assert state.get("last_night_research_day") == datetime.now(ns.IST).strftime("%Y-%m-%d")


def test_run_nightly_maintenance_skips_when_marker_already_set(monkeypatch, tmp_path):
    marker = tmp_path / "night_research_day.txt"
    run_key = datetime.now(ns.IST).strftime("%Y-%m-%d")
    marker.write_text(run_key, encoding="utf-8")
    monkeypatch.setattr(ns, "RUN_MARKER", str(marker))

    called = {"night_job": 0}

    def fake_night_job():
        called["night_job"] += 1
        return {"selected": ["ABC"], "details": {}}

    monkeypatch.setattr(ns.night_research, "run_night_job", fake_night_job)

    state = {}
    ns.run_nightly_maintenance(state)

    assert called["night_job"] == 0
    assert state.get("last_night_research_day") == run_key


def test_run_nightly_maintenance_force_bypasses_marker(monkeypatch, tmp_path):
    marker = tmp_path / "night_research_day.txt"
    run_key = datetime.now(ns.IST).strftime("%Y-%m-%d")
    marker.write_text(run_key, encoding="utf-8")
    monkeypatch.setattr(ns, "RUN_MARKER", str(marker))

    called = {"night_job": 0}

    def fake_night_job():
        called["night_job"] += 1
        return {"selected": ["ABC"], "details": {}}

    monkeypatch.setattr(ns.night_research, "run_night_job", fake_night_job)
    monkeypatch.setattr(ns.research_engine, "apply_night_universe", lambda *a, **k: {})
    monkeypatch.setattr(ns, "save_universe", lambda *a, **k: "x")

    state = {}
    ns.run_nightly_maintenance(state, force=True)

    assert called["night_job"] == 1
