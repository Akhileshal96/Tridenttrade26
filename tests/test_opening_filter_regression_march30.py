import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def test_missing_spread_volume_range_maps_to_moderate(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.12,
            "first_5m_range_pct": 0.0,
            "direction_clear": False,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": False,
            "feed_error": False,
        },
    )

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_MODERATE"
    assert metrics.get("reason") == "incomplete_opening_data"
    assert metrics.get("decision_path") == "incomplete_data"


def test_true_feed_exception_maps_to_hard_block_broken_feed(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.0,
            "first_5m_range_pct": 0.0,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": False,
            "feed_error": True,
            "data_state": "FEED_ERROR",
        },
    )

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_HARD_BLOCK"
    assert metrics.get("reason") == "confirmed_broken_feed"
    assert metrics.get("decision_path") == "feed_error"
    conf_meta = metrics.get("confidence_meta") or {}
    assert conf_meta.get("ignored") == []


def test_extreme_gap_maps_to_hard_block(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc.CFG, "MAX_SAFE_GAP_PCT", 0.8, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 1.7,
            "first_5m_range_pct": 0.6,
            "spread_quality": "GOOD",
            "volume_quality": "GOOD",
            "valid": True,
            "feed_error": False,
        },
    )

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_HARD_BLOCK"
    assert metrics.get("reason") == "confirmed_extreme_gap"
    assert metrics.get("decision_path") == "extreme_gap"


def test_incomplete_data_never_sets_broken_feed_reason(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.15,
            "first_5m_range_pct": 0.0,
            "spread_quality": "UNKNOWN",
            "volume_quality": "UNKNOWN",
            "valid": True,
            "feed_error": False,
        },
    )

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_MODERATE"
    assert metrics.get("reason") != "confirmed_broken_feed"
    assert metrics.get("decision_path") == "incomplete_data"


def test_short_rejection_reason_is_deterministic(monkeypatch):
    monkeypatch.setattr(
        tc,
        "_quality_metrics",
        lambda _s: {"ok": True, "price": 105.0, "sma20": 100.0, "sma20_prev": 99.0, "vol_score": 1.4, "rs_vs_nifty": -0.5},
    )
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    sig = tc.generate_short_signal("SBIN", strategy_family="short_breakdown")
    assert sig is None
    assert tc.STATE.get("last_short_reject_reasons", {}).get("SBIN") == "price_not_below_sma20"


def test_short_scan_records_symbol_level_skip_reason(monkeypatch):
    monkeypatch.setattr(tc, "_positions", lambda: {})
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    monkeypatch.setattr(
        tc,
        "_quality_metrics",
        lambda _s: {"ok": True, "price": 98.0, "sma20": 100.0, "sma20_prev": 101.0, "vol_score": 0.5, "rs_vs_nifty": -0.6},
    )

    rec = {}
    monkeypatch.setattr(tc.SA, "record_skipped_signal", lambda d: rec.update(d))
    monkeypatch.setattr(tc, "_maybe_enter_short_from_signal", lambda _sig: False)

    out = tc._scan_short_entries(["AXISBANK"], max_new=1, strategy_family="short_breakdown")
    assert out == 0
    assert rec.get("symbol") == "AXISBANK"
    assert rec.get("reason") == "volume_score_below_threshold"
