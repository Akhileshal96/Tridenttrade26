import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def _base_patch(monkeypatch):
    tc.STATE["research_universe"] = ["ATGL", "RELIANCE", "TCS"]
    tc.STATE["positions"] = {}
    tc.STATE["skip_cooldown"] = {}

    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_skip_cooldown_active", lambda _sym: False)
    monkeypatch.setattr(tc, "_passes_sector_entry_filter", lambda _sym: True)
    monkeypatch.setattr(tc, "_active_trade_universe", lambda: ["ATGL", "RELIANCE", "TCS"])
    monkeypatch.setattr(tc, "_calc_qty", lambda sym, entry: (10, 10, 10))
    monkeypatch.setattr(tc, "_can_open_new_trade", lambda *a, **k: True)
    monkeypatch.setattr(tc, "_compute_symbol_momentum_pct", lambda _sym: 1.0)
    monkeypatch.setattr(tc, "is_live_enabled", lambda: False)
    monkeypatch.setattr(tc, "_set_cooldown", lambda: None)
    monkeypatch.setattr(tc, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_sector_for_symbol", lambda _sym: "OTHER")



def test_weak_market_blocks_non_top_ranked(monkeypatch):
    _base_patch(monkeypatch)
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "WEAK"})
    monkeypatch.setattr(tc, "is_market_entry_allowed", lambda *a, **k: (False, "not_top_ranked", {}))

    called = {"reason": None}
    monkeypatch.setattr(tc, "_apply_skip_cooldown", lambda sym, reason, minutes=3: called.update({"reason": reason}))

    ok = tc._maybe_enter_from_signal({"symbol": "ATGL", "entry": 100.0})

    assert ok is False
    assert called["reason"] == "market_weak"


def test_weak_market_allowed_reduces_size(monkeypatch):
    _base_patch(monkeypatch)
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "WEAK"})
    monkeypatch.setattr(tc, "is_market_entry_allowed", lambda *a, **k: (True, "allowed", {"rank": 1, "score": 1.25}))
    monkeypatch.setattr(tc.CFG, "WEAK_MARKET_SIZE_MULTIPLIER", 0.5, raising=False)

    ok = tc._maybe_enter_from_signal({"symbol": "ATGL", "entry": 100.0})

    assert ok is True
    assert tc.STATE["positions"]["ATGL"]["qty"] == 5


def test_rank_helpers():
    uni = ["ATGL", "RELIANCE", "TCS"]
    assert tc.get_research_rank("ATGL", uni) == 1
    assert tc.is_top_ranked_symbol("RELIANCE", uni, top_n=2) is True
    assert tc.is_top_ranked_symbol("TCS", uni, top_n=2) is False
