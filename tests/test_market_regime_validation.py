import os
import sys

sys.path.insert(0, os.getcwd())

import pandas as pd
import market_regime as mr
import universe_builder as ub


def test_market_snapshot_returns_unknown_when_nifty_invalid(monkeypatch):
    mr._LAST_VALID_SNAPSHOT = None
    df = pd.DataFrame({"Close": [0.0] * 30, "High": [0.0] * 30, "Low": [0.0] * 30})
    monkeypatch.setattr(mr.yf, "download", lambda *a, **k: df)

    snap = mr.get_market_regime_snapshot()

    assert snap["regime"] == "UNKNOWN"
    assert snap["valid_data"] is False


def test_market_snapshot_uses_last_valid_fallback(monkeypatch):
    mr._LAST_VALID_SNAPSHOT = {
        "regime": "SIDEWAYS",
        "nifty": 22000.0,
        "ema20": 21800.0,
        "chg1": 0.3,
        "chg5": 1.2,
        "atr_pct": 1.1,
        "valid_data": True,
        "fallback_used": False,
        "fallback_source": "none",
    }
    monkeypatch.setattr(mr.yf, "download", lambda *a, **k: pd.DataFrame())

    snap = mr.get_market_regime_snapshot()

    assert snap["regime"] == "SIDEWAYS"
    assert snap["valid_data"] is False
    assert snap["fallback_source"] == "last_valid"


def test_is_market_regime_ok_does_not_block_unknown_by_default(monkeypatch):
    monkeypatch.setattr(ub, "get_market_regime_snapshot", lambda: {"regime": "UNKNOWN", "valid_data": False})
    monkeypatch.setattr(ub.CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", False, raising=False)

    assert ub.is_market_regime_ok() is True


def test_is_market_regime_ok_blocks_unknown_when_configured(monkeypatch):
    monkeypatch.setattr(ub, "get_market_regime_snapshot", lambda: {"regime": "UNKNOWN", "valid_data": False})
    monkeypatch.setattr(ub.CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", True, raising=False)

    assert ub.is_market_regime_ok() is False
