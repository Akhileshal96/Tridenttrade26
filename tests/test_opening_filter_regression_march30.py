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
