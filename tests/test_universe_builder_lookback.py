import os
import sys

sys.path.insert(0, os.getcwd())

import pandas as pd
import universe_builder as ub


def test_build_dynamic_universe_uses_one_year_lookback_by_default(monkeypatch):
    seen = {"period": None, "interval": None}

    monkeypatch.setattr(ub, "load_nifty100_symbols", lambda: ["RELIANCE", "INFY"])
    monkeypatch.setattr(ub, "load_excluded", lambda: [])

    def fake_download_cached(symbols, period="1y", interval="1d"):
        seen["period"] = period
        seen["interval"] = interval
        return pd.DataFrame()

    monkeypatch.setattr(ub, "_download_cached", fake_download_cached)

    details = ub.build_dynamic_universe_details(target_size=20)

    assert details["selected"] == []
    assert seen["period"] == "1y"
    assert seen["interval"] == "1d"


def test_build_dynamic_universe_respects_configured_lookback(monkeypatch):
    seen = {"period": None}

    monkeypatch.setattr(ub, "load_nifty100_symbols", lambda: ["RELIANCE", "INFY"])
    monkeypatch.setattr(ub, "load_excluded", lambda: [])
    monkeypatch.setattr(ub.CFG, "UNIVERSE_LOOKBACK_PERIOD", "18mo", raising=False)

    def fake_download_cached(symbols, period="1y", interval="1d"):
        seen["period"] = period
        return pd.DataFrame()

    monkeypatch.setattr(ub, "_download_cached", fake_download_cached)

    ub.build_dynamic_universe_details(target_size=20)

    assert seen["period"] == "18mo"
