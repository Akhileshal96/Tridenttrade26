import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def _base_state():
    tc.STATE["wallet_net_inr"] = 100000.0
    tc.STATE["wallet_available_inr"] = 100000.0
    tc.STATE["positions"] = {}
    tc.STATE["open_trades"] = tc.STATE["positions"]
    tc.STATE["no_entry_cycles"] = 0
    tc.STATE["signals_seen_window"] = 2
    tc.STATE["entries_executed_window"] = 0
    tc.STATE["reduce_size_factor"] = 1.0


def test_full_aligned_trade_gets_larger_qty_than_micro(monkeypatch):
    _base_state()
    monkeypatch.setattr(tc, "_current_exposure_inr", lambda: 0.0)
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    q_full, _, _ = tc._calc_qty("ABC", 100.0, tier="FULL", tier_weight=1.2, side="BUY", regime="TRENDING_UP", trend_direction="UP", family="trend_long")
    q_micro, _, _ = tc._calc_qty("ABC", 100.0, tier="MICRO", tier_weight=0.6, side="BUY", regime="UNKNOWN", trend_direction="UNKNOWN", family="mean_reversion")
    assert q_full > q_micro


def test_micro_trade_remains_constrained(monkeypatch):
    _base_state()
    monkeypatch.setattr(tc, "_current_exposure_inr", lambda: 0.0)
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    q_micro, _, _ = tc._calc_qty("ABC", 100.0, tier="MICRO", tier_weight=0.6, side="BUY", regime="VOLATILE", trend_direction="UNKNOWN", family="mean_reversion")
    assert q_micro >= 0
    assert q_micro <= 300


def test_short_aligned_not_blindly_overcut(monkeypatch):
    _base_state()
    monkeypatch.setattr(tc, "_current_exposure_inr", lambda: 0.0)
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    monkeypatch.setattr(tc, "_cfg_get", lambda k, d=None: (1.0 if k == "SHORT_SIZE_ALIGNED_MULT" else (0.5 if k == "SHORT_SIZE_NON_ALIGNED_MULT" else d)))
    q_aligned, _, _ = tc._calc_qty("ABC", 100.0, tier="FULL", tier_weight=1.0, side="SELL", regime="TRENDING_DOWN", trend_direction="DOWN", family="short_breakdown")
    q_non, _, _ = tc._calc_qty("ABC", 100.0, tier="FULL", tier_weight=1.0, side="SELL", regime="SIDEWAYS", trend_direction="UP", family="short_breakdown")
    assert q_aligned >= q_non


def test_exposure_and_risk_caps_still_limit_qty(monkeypatch):
    _base_state()
    tc.STATE["wallet_net_inr"] = 10000.0
    tc.STATE["wallet_available_inr"] = 10000.0
    monkeypatch.setattr(tc, "_current_exposure_inr", lambda: 5900.0)
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 1)
    q, capital_q, risk_q = tc._calc_qty("ABC", 100.0, tier="FULL", tier_weight=1.0, side="BUY", regime="TRENDING_UP", trend_direction="UP", family="trend_long")
    assert q <= capital_q
    assert q <= risk_q


def test_below_min_meaningful_notional_skips(monkeypatch):
    _base_state()
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_skip_cooldown_active", lambda _s: False)
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "TRENDING_UP", "trend_direction": "UP"})
    monkeypatch.setattr(tc, "is_market_entry_allowed", lambda *a, **k: (True, "ok", {}))
    monkeypatch.setattr(tc, "_active_trade_universe", lambda: ["INFY"])
    monkeypatch.setattr(tc, "_passes_sector_entry_filter", lambda _s: True)
    monkeypatch.setattr(tc, "_build_entry_confidence", lambda *a, **k: {"score": 85, "tier": "FULL", "size_mult": 1.0, "components": {}, "hard_block": ""})
    monkeypatch.setattr(tc, "_calc_qty", lambda *a, **k: (1, 1, 10))
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: False)
    monkeypatch.setattr(tc.CFG, "MIN_MEANINGFUL_NOTIONAL_INR", 500.0, raising=False)
    out = tc._maybe_enter_from_signal({"symbol": "INFY", "entry": 100.0, "strategy_family": "trend_long"})
    assert out is False
