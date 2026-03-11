import os
import sys

sys.path.insert(0, os.getcwd())

import research_engine as re


def _reset_state():
    re.research_state["research_universe"] = []
    re.research_state["trading_universe"] = []
    re.research_state["last_night_research"] = None
    re.research_state["last_refresh"] = None


def test_adaptive_refresh_limits_churn(monkeypatch):
    _reset_state()
    re.research_state["research_universe"] = ["A", "B", "C", "D", "E"]
    re.research_state["trading_universe"] = ["A", "B", "C", "D", "E"]

    monkeypatch.setattr(re.CFG, "UNIVERSE_SIZE", 5, raising=False)
    monkeypatch.setattr(re.CFG, "INTRADAY_DYNAMIC_REFRESH", True, raising=False)
    monkeypatch.setattr(re.CFG, "INTRADAY_REFRESH_MAX_SWAPS", 2, raising=False)
    monkeypatch.setattr(re, "build_dynamic_universe", lambda target_size=None: ["A", "X", "Y", "Z", "E"])
    monkeypatch.setattr(re, "append_log", lambda *args, **kwargs: None)

    out = re.refresh_top_movers_from_research()

    assert len(out) == 5
    # At most 2 newcomers due to swap cap
    newcomers = [s for s in out if s not in ["A", "B", "C", "D", "E"]]
    assert len(newcomers) <= 2


def test_adaptive_refresh_falls_back_when_empty(monkeypatch):
    _reset_state()
    re.research_state["research_universe"] = ["A", "B", "C"]

    monkeypatch.setattr(re.CFG, "UNIVERSE_SIZE", 2, raising=False)
    monkeypatch.setattr(re.CFG, "INTRADAY_DYNAMIC_REFRESH", True, raising=False)
    monkeypatch.setattr(re, "build_dynamic_universe", lambda target_size=None: [])

    out = re.refresh_top_movers_from_research()
    assert out == ["A", "B"]
