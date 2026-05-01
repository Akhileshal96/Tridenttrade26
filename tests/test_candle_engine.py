"""Tests for the Tier-1 candle filters in candle_engine.py.

Covers:
  • Single-candle pattern detectors (doji, hammer, shooting_star)
  • Two-candle patterns (engulfing, inside-bar)
  • check_reversal_against_entry (high-level veto)
  • check_volume_confirmation (volume vs rolling-avg)
  • is_within_candle_settling_window (fresh-bar guard)
  • check_candle_filters orchestrator
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import candle_engine as CE


IST = ZoneInfo("Asia/Kolkata")


def _candle(o, h, l, c, v=1000):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


# ============================================================================
# Single-candle pattern detectors
# ============================================================================

def test_doji_detected_when_body_is_tiny():
    # body = 0.02, range = 1.0 → ratio 2% < 10%
    assert CE.is_doji(_candle(100.00, 100.50, 99.50, 100.02))


def test_doji_not_detected_for_normal_body():
    # body = 0.50, range = 1.0 → 50% > 10%
    assert not CE.is_doji(_candle(100.00, 100.60, 100.00, 100.50))


def test_hammer_classic_bullish_reversal():
    # Strong lower wick, small body at top, small upper wick
    # open=100.5, close=101.0, low=99.0, high=101.2
    # body=0.5, lower_wick = 100.5-99 = 1.5 (= 3x body), upper_wick = 0.2
    c = _candle(100.5, 101.2, 99.0, 101.0)
    assert CE.is_hammer(c)


def test_hammer_rejected_when_upper_wick_too_long():
    # upper wick exceeds body — not a clean hammer
    c = _candle(100.5, 102.0, 99.0, 101.0)
    assert not CE.is_hammer(c)


def test_shooting_star_classic_bearish_reversal():
    # open=100, high=110, low=99, close=99.1
    # body=0.9, upper_wick=10 (11x body), lower_wick=0.1 (clearly < body), close_pos≈0.9%
    c = _candle(100.0, 110.0, 99.0, 99.1)
    assert CE.is_shooting_star(c)


def test_shooting_star_rejected_when_lower_wick_too_long():
    # body=0.9, lower_wick=1.5 (clearly > body) → not a shooting star
    c = _candle(100.0, 110.0, 97.6, 99.1)
    assert not CE.is_shooting_star(c)


def test_bullish_engulfing():
    prev = _candle(102.0, 102.5, 100.0, 100.5)  # red, body 1.5
    curr = _candle(100.0, 103.0, 99.5, 102.8)   # green body engulfs prev red body
    assert CE.is_bullish_engulfing(prev, curr)


def test_bearish_engulfing():
    prev = _candle(100.0, 102.0, 99.5, 101.5)   # green
    curr = _candle(102.0, 102.5, 99.0, 99.5)    # red, engulfs
    assert CE.is_bearish_engulfing(prev, curr)


def test_inside_bar():
    prev = _candle(100.0, 105.0, 95.0, 102.0)   # wide range
    curr = _candle(101.0, 103.0, 98.0, 100.5)   # entirely within prev
    assert CE.is_inside_bar(prev, curr)


def test_inside_bar_negative_when_break_high():
    prev = _candle(100.0, 105.0, 95.0, 102.0)
    curr = _candle(101.0, 106.0, 98.0, 100.5)   # high exceeds prev
    assert not CE.is_inside_bar(prev, curr)


# ============================================================================
# High-level reversal-against-entry veto
# ============================================================================

def test_short_blocked_on_bullish_hammer():
    candles = [_candle(100.5, 101.2, 99.0, 101.0)]
    block, reason = CE.check_reversal_against_entry(candles, "SHORT")
    assert block is True
    assert reason == "bullish_hammer"


def test_long_blocked_on_shooting_star():
    # Same clean shooting-star data as the standalone test
    candles = [_candle(100.0, 110.0, 99.0, 99.1)]
    block, reason = CE.check_reversal_against_entry(candles, "BUY")
    assert block is True
    assert reason == "bearish_shooting_star"


def test_short_blocked_on_bullish_engulfing():
    candles = [
        _candle(102.0, 102.5, 100.0, 100.5),   # prev red
        _candle(100.0, 103.0, 99.5, 102.8),    # curr green engulfing
    ]
    block, reason = CE.check_reversal_against_entry(candles, "SHORT")
    assert block is True
    assert reason == "bullish_engulfing"


def test_short_blocked_on_doji():
    candles = [_candle(100.0, 100.50, 99.50, 100.02)]
    block, reason = CE.check_reversal_against_entry(candles, "SHORT")
    assert block is True
    assert reason == "doji_indecision"


def test_short_allowed_on_clean_bearish_candle():
    # Wide red candle, no wicks — bearish continuation, NOT a reversal
    candles = [_candle(100.0, 100.1, 98.0, 98.1)]
    block, reason = CE.check_reversal_against_entry(candles, "SHORT")
    assert block is False
    assert reason == "ok"


def test_inside_bar_blocks_either_direction():
    candles = [
        _candle(100.0, 105.0, 95.0, 102.0),
        _candle(101.0, 103.0, 98.0, 100.5),
    ]
    block_short, _ = CE.check_reversal_against_entry(candles, "SHORT")
    block_long, _ = CE.check_reversal_against_entry(candles, "BUY")
    assert block_short and block_long


def test_no_data_fails_open():
    """Empty candles → caller should treat as "no data, allow"."""
    block, reason = CE.check_reversal_against_entry([], "SHORT")
    assert block is False
    assert reason == "no_data"


# ============================================================================
# Volume confirmation
# ============================================================================

def test_volume_confirms_when_latest_meets_threshold():
    # 20 prior candles avg=1000, last=1500 → ratio 1.5 → meets 1.5x
    candles = [_candle(100, 101, 99, 100, v=1000) for _ in range(20)]
    candles.append(_candle(100, 101, 99, 100, v=1500))
    confirms, ratio = CE.check_volume_confirmation(candles, multiplier=1.5)
    assert confirms is True
    assert abs(ratio - 1.5) < 0.001


def test_volume_does_not_confirm_when_below_threshold():
    candles = [_candle(100, 101, 99, 100, v=1000) for _ in range(20)]
    candles.append(_candle(100, 101, 99, 100, v=1100))
    confirms, ratio = CE.check_volume_confirmation(candles, multiplier=1.5)
    assert confirms is False
    assert abs(ratio - 1.1) < 0.001


def test_volume_returns_zero_for_insufficient_history():
    candles = [_candle(100, 101, 99, 100, v=1000) for _ in range(5)]
    confirms, ratio = CE.check_volume_confirmation(candles, multiplier=1.5)
    assert confirms is False
    assert ratio == 0.0


# ============================================================================
# Fresh-candle settling guard
# ============================================================================

def test_settling_window_active_in_first_30s():
    # 09:35:15 — 15s into the 5-min candle that started at 09:35:00
    now = datetime(2026, 5, 1, 9, 35, 15, tzinfo=IST)
    assert CE.is_within_candle_settling_window(seconds=60, interval_min=5, now=now)


def test_settling_window_inactive_after_60s():
    # 09:36:30 — 90s into the candle
    now = datetime(2026, 5, 1, 9, 36, 30, tzinfo=IST)
    assert not CE.is_within_candle_settling_window(seconds=60, interval_min=5, now=now)


def test_settling_window_disabled_when_seconds_zero():
    now = datetime(2026, 5, 1, 9, 35, 15, tzinfo=IST)
    assert not CE.is_within_candle_settling_window(seconds=0, interval_min=5, now=now)


def test_settling_window_at_exact_boundary():
    # 09:35:00 exactly — the moment the new candle opens. Should be within window.
    now = datetime(2026, 5, 1, 9, 35, 0, tzinfo=IST)
    assert CE.is_within_candle_settling_window(seconds=60, interval_min=5, now=now)


# ============================================================================
# check_candle_filters orchestrator
# ============================================================================

def test_orchestrator_returns_disabled_when_master_off(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", False, raising=False)
    allow, reason, ctx = CE.check_candle_filters("INFY", "BUY")
    assert allow is True
    assert reason == "filters_disabled"


def test_orchestrator_settling_window_blocks(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", True, raising=False)
    monkeypatch.setattr(CFG, "USE_FRESH_CANDLE_GUARD", True, raising=False)
    monkeypatch.setattr(CFG, "FRESH_CANDLE_GUARD_SEC", 60, raising=False)
    monkeypatch.setattr(CFG, "CANDLE_PATTERN_INTERVAL", "5minute", raising=False)
    # Force the settling-window helper to return True regardless of real clock
    monkeypatch.setattr(CE, "is_within_candle_settling_window", lambda **k: True)
    allow, reason, ctx = CE.check_candle_filters("INFY", "BUY")
    assert allow is False
    assert reason == "settling_window"


def test_orchestrator_no_data_fails_open(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", True, raising=False)
    monkeypatch.setattr(CFG, "USE_FRESH_CANDLE_GUARD", False, raising=False)
    monkeypatch.setattr(CE, "fetch_candles", lambda *a, **k: [])
    allow, reason, ctx = CE.check_candle_filters("INFY", "BUY")
    assert allow is True
    assert reason == "no_candle_data"


def test_orchestrator_blocks_on_reversal(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", True, raising=False)
    monkeypatch.setattr(CFG, "USE_FRESH_CANDLE_GUARD", False, raising=False)
    monkeypatch.setattr(CFG, "USE_REVERSAL_CANDLE_VETO", True, raising=False)
    monkeypatch.setattr(CFG, "USE_VOLUME_CONFIRMATION", False, raising=False)
    # Hammer candle → SHORT must be blocked
    fake_candles = [_candle(100.5, 101.2, 99.0, 101.0)]
    monkeypatch.setattr(CE, "fetch_candles", lambda *a, **k: fake_candles)
    allow, reason, ctx = CE.check_candle_filters("INFY", "SHORT")
    assert allow is False
    assert reason.startswith("reversal_")


def test_orchestrator_blocks_on_low_volume(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", True, raising=False)
    monkeypatch.setattr(CFG, "USE_FRESH_CANDLE_GUARD", False, raising=False)
    monkeypatch.setattr(CFG, "USE_REVERSAL_CANDLE_VETO", False, raising=False)
    monkeypatch.setattr(CFG, "USE_VOLUME_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(CFG, "VOLUME_CONFIRMATION_MULT", 1.5, raising=False)
    fake_candles = [_candle(100, 101, 99, 100, v=1000) for _ in range(20)]
    fake_candles.append(_candle(100, 101, 99, 100, v=900))  # below avg
    monkeypatch.setattr(CE, "fetch_candles", lambda *a, **k: fake_candles)
    allow, reason, ctx = CE.check_candle_filters("INFY", "BUY")
    assert allow is False
    assert reason.startswith("volume_below_")
    assert ctx["volume_ratio"] < 1.5


def test_orchestrator_allows_when_all_clear(monkeypatch):
    monkeypatch.setattr(CFG, "USE_CANDLE_FILTERS", True, raising=False)
    monkeypatch.setattr(CFG, "USE_FRESH_CANDLE_GUARD", False, raising=False)
    monkeypatch.setattr(CFG, "USE_REVERSAL_CANDLE_VETO", True, raising=False)
    monkeypatch.setattr(CFG, "USE_VOLUME_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(CFG, "VOLUME_CONFIRMATION_MULT", 1.5, raising=False)
    # 20 prior candles + clean bearish current candle with high volume → SHORT allowed
    fake_candles = [_candle(100, 101, 99, 100, v=1000) for _ in range(20)]
    fake_candles.append(_candle(100, 100.1, 98, 98.1, v=2000))  # bearish, 2x vol
    monkeypatch.setattr(CE, "fetch_candles", lambda *a, **k: fake_candles)
    allow, reason, ctx = CE.check_candle_filters("INFY", "SHORT")
    assert allow is True
    assert reason == "ok"
    assert ctx["volume_ratio"] >= 1.5


# ============================================================================
# Cache TTL behavior
# ============================================================================

def test_cache_clear_resets_state():
    CE._CANDLE_CACHE[("FAKE", "5minute")] = (0.0, [_candle(1, 2, 0, 1)])
    CE.clear_cache()
    assert CE._CANDLE_CACHE == {}
