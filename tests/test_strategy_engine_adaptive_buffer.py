import os
import sys

import pandas as pd

sys.path.insert(0, os.getcwd())

import strategy_engine as se


class _KiteStub:
    def __init__(self, rows):
        self._rows = rows

    def historical_data(self, *_a, **_k):
        return self._rows


def test_generate_signal_requires_buffer_break(monkeypatch):
    closes = [100.0] * 24 + [100.05]
    rows = [{"close": c, "high": c + 0.1, "low": c - 0.1} for c in closes]
    monkeypatch.setattr(se, "get_kite", lambda: _KiteStub(rows))
    monkeypatch.setattr(se, "token_for_symbol", lambda _s: 1)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(se.CFG, "SMA20_ENTRY_BUFFER_PCT", 0.1, raising=False)

    out = se.generate_signal(["ABC"])
    assert out is None


def test_generate_mean_reversion_signal_rsi_setup(monkeypatch):
    closes = [120.0 - i for i in range(25)]
    rows = [{"close": c, "high": c + 0.1, "low": c - 0.1} for c in closes]
    monkeypatch.setattr(se, "get_kite", lambda: _KiteStub(rows))
    monkeypatch.setattr(se, "token_for_symbol", lambda _s: 1)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)

    sig = se.generate_mean_reversion_signal(["XYZ"])
    assert sig is not None
    assert sig["strategy_setup"] == "mean_reversion"
