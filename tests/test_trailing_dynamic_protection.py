import os
import sys

sys.path.insert(0, os.getcwd())

import execution_engine as ee


def _mk_trade(tier: str):
    return {"entry_price": 100.0, "quantity": 10, "side": "LONG", "confidence_tier": tier}


def test_trail_activation_threshold_full():
    assert ee._calc_trail_activate_inr(100.0, 10, "FULL") == 8.0


def test_trail_activation_threshold_reduced():
    assert ee._calc_trail_activate_inr(100.0, 10, "REDUCED") == 3.0


def test_trail_activation_threshold_micro():
    assert ee._calc_trail_activate_inr(100.0, 10, "MICRO") == 2.0


def test_breakeven_lock_after_small_profit_reduced(monkeypatch):
    positions = {"ABC": _mk_trade("REDUCED")}
    monkeypatch.setattr(ee.CFG, "TRAIL_BREAKEVEN_ARM_INR", 4.5, raising=False)
    monkeypatch.setattr(ee.CFG, "TRAIL_BREAKEVEN_LOCK_INR", 0.2, raising=False)

    closes = []
    prices = iter([100.6, 99.9])  # +6 then -1

    def _get_ltp(_sym):
        return next(prices)

    def _close(sym, reason="MANUAL", ltp_override=None):
        closes.append((sym, reason))
        positions.pop(sym, None)
        return True

    ee.monitor_positions({}, positions, _get_ltp, _close, lambda: False)
    ee.monitor_positions({}, positions, _get_ltp, _close, lambda: False)
    assert closes and closes[-1][1] == "BREAKEVEN_LOCK"


def test_short_side_trailing_still_works(monkeypatch):
    positions = {
        "XYZ": {
            "entry_price": 100.0,
            "quantity": 10,
            "side": "SHORT",
            "confidence_tier": "FULL",
            "trail_active": True,
            "peak_pnl_inr": 20.0,
        }
    }
    monkeypatch.setattr(ee.CFG, "TRAIL_LOCK_RATIO", 0.5, raising=False)
    monkeypatch.setattr(ee.CFG, "TRAIL_BUFFER_INR", 1.0, raising=False)

    closes = []

    def _close(sym, reason="MANUAL", ltp_override=None):
        closes.append((sym, reason))
        positions.pop(sym, None)
        return True

    ee.monitor_positions({}, positions, lambda _s: 99.9, _close, lambda: False)  # pnl 1, below trigger 9
    assert closes and closes[-1][1] == "TRAIL"


def test_small_short_uses_lower_trail_floor(monkeypatch):
    monkeypatch.setattr(ee.CFG, "TRAIL_ACTIVATE_FULL_FLOOR_INR", 8.0, raising=False)
    monkeypatch.setattr(ee.CFG, "SHORT_SMALL_POSITION_VALUE_INR", 8000.0, raising=False)
    monkeypatch.setattr(ee.CFG, "SHORT_SMALL_TRAIL_FLOOR_INR", 3.0, raising=False)
    v = ee._calc_trail_activate_inr(100.0, 10, "FULL", side="SHORT")
    assert v <= 4.0
