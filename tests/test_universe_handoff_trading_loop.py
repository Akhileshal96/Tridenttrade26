import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def test_resolve_trade_universe_prefers_state(monkeypatch):
    tc.STATE["research_universe"] = ["RELIANCE", "TCS"]
    monkeypatch.setattr(tc, "load_excluded", lambda: [])
    monkeypatch.setattr(tc, "_load_research_universe_from_file", lambda: ["SHOULD_NOT_USE"])
    monkeypatch.setattr(tc, "load_universe_trading", lambda: ["STATIC"])

    out = tc._resolve_trade_universe()

    assert out == ["RELIANCE", "TCS"]


def test_resolve_trade_universe_loads_from_file_when_state_empty(monkeypatch):
    tc.STATE["research_universe"] = []
    monkeypatch.setattr(tc, "load_excluded", lambda: [])
    monkeypatch.setattr(tc, "_load_research_universe_from_file", lambda: ["INFY", "HDFCBANK"])
    monkeypatch.setattr(tc, "load_universe_trading", lambda: ["STATIC"])

    out = tc._resolve_trade_universe()

    assert out == ["INFY", "HDFCBANK"]


def test_resolve_trade_universe_falls_back_static_last(monkeypatch):
    tc.STATE["research_universe"] = []
    monkeypatch.setattr(tc, "load_excluded", lambda: [])
    monkeypatch.setattr(tc, "_load_research_universe_from_file", lambda: [])
    monkeypatch.setattr(tc, "load_universe_trading", lambda: ["SBIN", "ITC"])

    out = tc._resolve_trade_universe()

    assert out == ["SBIN", "ITC"]


def test_market_weak_applies_symbol_skip_cooldown(monkeypatch):
    tc.STATE["research_universe"] = ["ABC"]
    tc.STATE["skip_cooldown"] = {}
    tc.STATE["positions"] = {}

    monkeypatch.setattr(tc, "is_market_regime_ok", lambda: False)
    monkeypatch.setattr(tc, "_passes_sector_entry_filter", lambda _sym: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    called = {"reason": None, "minutes": None}

    def fake_apply(sym, reason, minutes=3):
        called["reason"] = reason
        called["minutes"] = minutes

    monkeypatch.setattr(tc, "_apply_skip_cooldown", fake_apply)
    monkeypatch.setattr(tc.CFG, "MARKET_WEAK_COOLDOWN_MIN", 4, raising=False)

    ok = tc._maybe_enter_from_signal({"symbol": "ABC", "entry": 100.0})

    assert ok is False
    assert called["reason"] == "market_weak"
    assert called["minutes"] == 4
