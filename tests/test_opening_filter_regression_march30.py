import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc
import strategy_engine as se
import pandas as pd


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


def test_true_feed_exception_softens_before_hard_block(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "_cfg_get", lambda k, d=None: (3 if k == "OPEN_FEED_HARD_BLOCK_RETRIES" else d))
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    tc.STATE["open_feed_retry_count"] = 0
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
    assert mode == "OPEN_MODERATE"
    assert metrics.get("reason") == "transient_feed_error_soft"
    assert metrics.get("decision_path") == "feed_error"
    assert metrics.get("data_state") == "FEED_ERROR_RETRYING"
    conf_meta = metrics.get("confidence_meta") or {}
    assert conf_meta.get("ignored") == []
    assert int(tc.STATE.get("open_feed_retry_count") or 0) == 1


def test_repeated_feed_exception_maps_to_hard_block(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "_cfg_get", lambda k, d=None: (3 if k == "OPEN_FEED_HARD_BLOCK_RETRIES" else d))
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    tc.STATE["open_feed_retry_count"] = 2
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
            "feed_exception": "timeout",
        },
    )

    mode, metrics = tc.get_opening_mode()
    assert mode == "OPEN_HARD_BLOCK"
    assert metrics.get("reason") == "confirmed_broken_feed"
    assert metrics.get("decision_path") == "feed_error"
    assert metrics.get("data_state") == "FEED_ERROR_CONFIRMED"


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


def test_incomplete_opening_data_never_reports_confidence_100(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 0.05,
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
    assert int(metrics.get("confidence") or 0) < 100


def test_scan_long_records_deterministic_skip_reason(monkeypatch):
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    monkeypatch.setattr(tc, "_positions", lambda: {})
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "generate_vwap_ema_signal", lambda _u: None)
    monkeypatch.setattr(tc, "generate_mean_reversion_signal", lambda _u: None)
    monkeypatch.setattr(tc, "_maybe_enter_from_signal", lambda _sig: False)

    rec = {}
    monkeypatch.setattr(tc.SA, "record_skipped_signal", lambda d: rec.update(d))
    se.LAST_SIGNAL_REJECT_REASONS["INFY"] = "mean_reversion_conditions_not_met"

    out = tc._scan_long_entries(["INFY"], max_new=1, signal_fn=lambda _u: None, strategy_family="mean_reversion")
    assert out == 0
    assert rec.get("symbol") == "INFY"
    assert rec.get("reason") == "mean_reversion_conditions_not_met"


def test_opening_metrics_handles_multiindex_without_ambiguous_truth(monkeypatch):
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["^NSEI"]])
    d1 = pd.DataFrame(
        [[100, 101, 99, 100, 1000], [101, 103, 100, 102, 1500]],
        columns=cols,
        index=pd.date_range("2026-04-06", periods=2, freq="D"),
    )
    m5 = pd.DataFrame(
        [[101, 102, 100, 101.5, 600], [101.5, 102, 101, 101.8, 650], [101.8, 102.2, 101.4, 102.0, 700]],
        columns=cols,
        index=pd.date_range("2026-04-06 03:45:00", periods=3, freq="5min", tz="UTC"),
    )
    monkeypatch.setattr(tc.yf, "download", lambda *a, **k: d1 if k.get("interval") == "1d" else m5)
    out = tc._compute_opening_metrics()
    assert out.get("feed_error") is False
    assert out.get("data_state") in ("READY", "INCOMPLETE")
