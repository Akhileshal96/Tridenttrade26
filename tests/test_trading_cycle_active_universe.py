import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import trading_cycle as tc

IST = ZoneInfo("Asia/Kolkata")


def test_confirm_long_htf_early_session_relaxed(monkeypatch):
    import pandas as pd

    # Price above MA while MA is not rising; should pass only in EARLY bucket.
    df = pd.DataFrame(
        {
            "close": [
                86.17, 104.27, 115.34, 102.08, 108.79, 108.40, 111.85, 95.43, 86.74, 114.93,
                119.17, 99.12, 98.31, 84.03, 106.40, 111.26, 115.84, 84.62, 119.44, 94.60,
                99.74, 117.50, 103.97, 89.07, 91.67, 118.64, 85.81, 87.89, 96.64, 114.24,
            ]
        }
    )
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "_session_bucket", lambda *_a, **_k: "EARLY")
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)

    assert tc.confirm_long_htf("ABC") is True


def test_build_active_universe_uses_scored_ranking(monkeypatch):
    monkeypatch.setattr(tc.CFG, "ACTIVE_UNIVERSE_SIZE", 2, raising=False)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_sector_strength_snapshot", lambda _u: {"OTHER": 1.0})

    scores = {"A": {"ok": True, "score": 1.0}, "B": {"ok": True, "score": 3.0}, "C": {"ok": True, "score": 2.0}}
    monkeypatch.setattr(tc, "_active_score_metrics", lambda sym, _ss: scores[sym])

    out = tc.build_active_universe(["A", "B", "C"])
    assert out == ["B", "C"]


def test_refresh_active_universe_if_due(monkeypatch):
    monkeypatch.setattr(tc.CFG, "ACTIVE_UNIVERSE_REFRESH_MINUTES", 10, raising=False)
    monkeypatch.setattr(tc, "build_active_universe", lambda u: ["X", "Y"])

    tc.STATE["active_universe"] = ["A"]
    tc.STATE["active_universe_last_refresh"] = datetime.now(IST) - timedelta(minutes=11)

    out = tc.refresh_active_universe_if_due(["A", "B"])
    assert out == ["X", "Y"]


def test_opening_mode_hard_block_from_confirmed_extreme_gap(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 1.5,
            "first_5m_range_pct": 2.0,
            "direction_clear": False,
            "spread_quality": "WIDE",
            "volume_quality": "LOW",
            "valid": True,
        },
    )
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    mode, _m = tc.get_opening_mode()
    assert mode == "OPEN_HARD_BLOCK"




def test_opening_mode_unknown_quality_defaults_to_moderate(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.10,
            "first_5m_range_pct": 0.0,
            "direction_clear": False,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": False,
        },
    )
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_MODERATE"
    assert metrics.get("reason") == "incomplete_opening_data"


def test_opening_mode_unknown_inputs_map_to_moderate_even_if_valid(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.12,
            "first_5m_range_pct": 0.4,
            "direction_clear": True,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": True,
            "feed_error": False,
        },
    )
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_MODERATE"
    assert metrics.get("reason") == "incomplete_opening_data"

def test_tick_allows_selective_scans_on_open_unsafe(monkeypatch):
    tc.STATE["paused"] = False
    tc.STATE["positions"] = {}
    tc.STATE["fallback_mode_active"] = False
    tc.STATE["no_entry_cycles"] = 0

    monkeypatch.setattr(tc, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(tc.RISK, "sync_wallet", lambda _s: None)
    monkeypatch.setattr(tc, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(tc.RISK, "check_day_drawdown_guard", lambda _s: False)
    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "_within_entry_window", lambda: True)
    monkeypatch.setattr(tc, "_resolve_trade_universe", lambda: ["A", "B", "C"])
    monkeypatch.setattr(tc, "refresh_active_universe_if_due", lambda _u: ["A", "B"])
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "TRENDING"})
    monkeypatch.setattr(tc, "get_regime_entry_mode", lambda _r: "LONG")
    monkeypatch.setattr(tc, "get_opening_mode", lambda: ("OPEN_UNSAFE", {}))
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "ee_monitor_positions", lambda *a, **k: None)

    called = {"long": 0, "short": 0}
    monkeypatch.setattr(tc, "_scan_long_entries", lambda *a, **k: called.__setitem__("long", called["long"] + 1) or 0)
    monkeypatch.setattr(tc, "_scan_short_entries", lambda *a, **k: called.__setitem__("short", called["short"] + 1) or 0)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    tc.tick()

    assert called["long"] >= 1
    assert called["short"] >= 0


def test_opening_moderate_blocks_non_top_ranked_symbol(monkeypatch):
    monkeypatch.setattr(tc.CFG, "OPEN_MODERATE_TOP_N", 2, raising=False)
    tc.STATE["opening_mode"] = "OPEN_MODERATE"
    monkeypatch.setattr(tc, "_active_trade_universe", lambda: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(tc, "_opening_symbol_quality_ok", lambda *_a, **_k: True)

    ok, reason = tc._opening_selective_entry_allowed("CCC", side="BUY")
    assert ok is False
    assert reason == "opening_filter_low_confidence"


def test_opening_confidence_ignores_unknown_inputs(monkeypatch):
    monkeypatch.setattr(tc.CFG, "MAX_SAFE_GAP_PCT", 0.8, raising=False)
    monkeypatch.setattr(tc.CFG, "MAX_SAFE_FIRST_5M_RANGE_PCT", 1.2, raising=False)

    score, meta = tc.get_opening_confidence(
        {
            "gap_pct": 0.15,
            "first_5m_range_pct": 0.0,
            "direction_clear": False,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": False,
            "feed_error": False,
        }
    )

    assert score >= 40
    assert "volume" in meta.get("ignored", [])
    assert "spread" in meta.get("ignored", [])


def test_opening_mode_uses_confidence_thresholds(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    monkeypatch.setattr(tc, "_compute_opening_metrics", lambda: {"gap_pct": 0.1, "feed_error": False, "valid": True, "spread_quality": "TIGHT", "volume_quality": "OK"})
    monkeypatch.setattr(tc, "get_opening_confidence", lambda _m=None: (35, {"ignored": []}))
    mode, _ = tc.get_opening_mode()
    assert mode == "OPEN_UNSAFE"

    monkeypatch.setattr(tc, "get_opening_confidence", lambda _m=None: (55, {"ignored": []}))
    mode, _ = tc.get_opening_mode()
    assert mode == "OPEN_MODERATE"

    monkeypatch.setattr(tc, "get_opening_confidence", lambda _m=None: (75, {"ignored": []}))
    mode, _ = tc.get_opening_mode()
    assert mode == "OPEN_CLEAN"


def test_opening_filter_fallback_min_trade_after_no_exec(monkeypatch):
    tc.STATE["opening_mode"] = "OPEN_MODERATE"
    tc.STATE["no_entry_cycles"] = 10
    monkeypatch.setattr(tc.CFG, "OPEN_MIN_TRADE_AFTER_NO_EXEC_CYCLES", 8, raising=False)
    monkeypatch.setattr(tc.CFG, "OPEN_MODERATE_TOP_N", 2, raising=False)
    monkeypatch.setattr(tc, "_active_trade_universe", lambda: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(tc, "_opening_symbol_quality_ok", lambda *_a, **_k: False)

    ok, reason = tc._opening_selective_entry_allowed("CCC", side="BUY")
    assert ok is True
    assert reason == "fallback_min_trade"


def test_tick_fallback_scans_mean_reversion_strategy(monkeypatch):
    tc.STATE["paused"] = False
    tc.STATE["positions"] = {}
    tc.STATE["fallback_mode_active"] = True
    tc.STATE["fallback_universe"] = ["AAA"]
    tc.STATE["no_entry_cycles"] = 10
    tc.STATE["active_no_setup_cycles"] = 0

    monkeypatch.setattr(tc, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(tc.RISK, "sync_wallet", lambda _s: None)
    monkeypatch.setattr(tc, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(tc.RISK, "check_day_drawdown_guard", lambda _s: False)
    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "_within_entry_window", lambda: True)
    monkeypatch.setattr(tc, "_resolve_trade_universe", lambda: ["A", "B"])
    monkeypatch.setattr(tc, "refresh_active_universe_if_due", lambda _u: ["A", "B"])
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "SIDEWAYS"})
    monkeypatch.setattr(tc, "get_regime_entry_mode", lambda _r: "BALANCED")
    monkeypatch.setattr(tc, "get_opening_mode", lambda: ("OPEN_CLEAN", {}))
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: False)
    monkeypatch.setattr(tc, "ee_monitor_positions", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_scan_short_entries", lambda *a, **k: 0)

    called = {"fallback_mr": False}

    def _scan_long_stub(universe, max_new, signal_fn=tc.generate_signal, strategy_family="trend_long", universe_source="primary"):
        if signal_fn is tc.generate_mean_reversion_signal:
            called["fallback_mr"] = True
        return 0

    monkeypatch.setattr(tc, "_scan_long_entries", _scan_long_stub)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    tc.tick()
    assert called["fallback_mr"] is True
