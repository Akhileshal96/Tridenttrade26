import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def test_weak_down_short_primary_relaxes_volume_threshold(monkeypatch):
    tc.STATE["last_regime"] = "WEAK"
    tc.STATE["last_trend_direction"] = "DOWN"
    monkeypatch.setattr(tc, "get_regime_entry_mode", lambda _r: "SHORT_PRIMARY")
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_quality_metrics",
        lambda _s: {"ok": True, "price": 99.95, "sma20": 100.0, "sma20_prev": 100.2, "vol_score": 1.12, "rs_vs_nifty": -0.5},
    )
    monkeypatch.setattr(tc.CFG, "SHORT_MIN_VOLUME_SCORE", 1.2, raising=False)
    monkeypatch.setattr(tc.CFG, "SHORT_WEAK_DOWN_VOL_RELAX_FACTOR", 0.9, raising=False)
    monkeypatch.setattr(tc.CFG, "SHORT_SMA20_TOLERANCE_PCT", 0.12, raising=False)
    sig = tc.generate_short_signal("INFY", strategy_family="short_breakdown")
    assert sig is not None


def test_volume_relaxation_not_applied_outside_weak_down(monkeypatch):
    tc.STATE["last_regime"] = "SIDEWAYS"
    tc.STATE["last_trend_direction"] = "UP"
    monkeypatch.setattr(tc, "get_regime_entry_mode", lambda _r: "BALANCED")
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_quality_metrics",
        lambda _s: {"ok": True, "price": 99.95, "sma20": 100.0, "sma20_prev": 100.2, "vol_score": 1.12, "rs_vs_nifty": -0.5},
    )
    monkeypatch.setattr(tc.CFG, "SHORT_MIN_VOLUME_SCORE", 1.2, raising=False)
    sig = tc.generate_short_signal("INFY", strategy_family="short_breakdown")
    assert sig is None


def test_final_qty_reason_chain_logged(monkeypatch):
    tc.STATE["wallet_net_inr"] = 100000.0
    tc.STATE["wallet_available_inr"] = 100000.0
    tc.STATE["positions"] = {}
    tc.STATE["open_trades"] = tc.STATE["positions"]
    monkeypatch.setattr(tc, "_current_exposure_inr", lambda: 0.0)
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    logs = []
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: logs.append(a))
    tc._calc_qty("SBIN", 100.0, tier="FULL", tier_weight=1.0, side="SELL", regime="SIDEWAYS", trend_direction="UP", family="short_breakdown")
    assert any("final_qty_reason_chain=" in str(x[-1]) for x in logs if len(x) >= 3 and x[1] == "SIZE")
