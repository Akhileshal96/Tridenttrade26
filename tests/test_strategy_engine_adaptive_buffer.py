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


def test_generate_signal_skips_only_when_core_history_missing(monkeypatch):
    rows = [{"close": 100.0, "high": 100.1, "low": 99.9} for _ in range(19)]
    logs = []
    monkeypatch.setattr(se, "get_kite", lambda: _KiteStub(rows))
    monkeypatch.setattr(se, "token_for_symbol", lambda _s: 1)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(se, "append_log", lambda *a, **k: logs.append(" ".join(str(x) for x in a)))

    out = se.generate_signal(["ABC"])
    assert out is None
    assert any("skipped reason=insufficient_history_min_bars" in x for x in logs)


def test_generate_signal_partial_eval_when_atr_history_missing(monkeypatch):
    rows = [{"close": 100.0 + (0.02 * i)} for i in range(20)]
    logs = []
    monkeypatch.setattr(se, "get_kite", lambda: _KiteStub(rows))
    monkeypatch.setattr(se, "token_for_symbol", lambda _s: 1)
    monkeypatch.setattr(se.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(se, "append_log", lambda *a, **k: logs.append(" ".join(str(x) for x in a)))
    monkeypatch.setattr(se.CFG, "SMA20_ENTRY_BUFFER_ATR_MULT", 1.0, raising=False)

    _ = se.generate_signal(["ABC"])
    assert any("partial_eval reason=insufficient_history_for_indicator indicator=ATR" in x for x in logs)
