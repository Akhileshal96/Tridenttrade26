import os
import sys

sys.path.insert(0, os.getcwd())

import pandas as pd
import universe_builder as ub


def _mk_sdf(candles=230, close_start=100.0, step=0.6, volume=2_000_000):
    close = [close_start + i * step for i in range(candles)]
    high = [c * 1.01 for c in close]
    low = [c * 0.99 for c in close]
    vol = [volume] * candles
    return pd.DataFrame({"Close": close, "High": high, "Low": low, "Volume": vol})


def test_dynamic_universe_uses_partial_scoring_when_rs_available_but_others_missing(monkeypatch):
    monkeypatch.setattr(ub, "load_nifty100_symbols", lambda: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(ub, "load_excluded", lambda: [])
    monkeypatch.setattr(ub, "save_universe", lambda symbols, path=None: path or "x")

    cols = pd.MultiIndex.from_product([["AAA.NS", "BBB.NS", "CCC.NS"], ["Close", "High", "Low", "Volume"]])
    df = pd.DataFrame(columns=cols)
    # not used directly because we patch hist[tk] access by returning valid frames via __getitem__ from DataFrame slices

    # Build combined multiindex dataframe with each ticker data
    parts = []
    for tk in ["AAA.NS", "BBB.NS", "CCC.NS"]:
        sdf = _mk_sdf()
        sdf.columns = pd.MultiIndex.from_product([[tk], sdf.columns])
        parts.append(sdf)
    hist = pd.concat(parts, axis=1)

    monkeypatch.setattr(ub, "_download_cached", lambda *a, **k: hist)

    nifty = pd.DataFrame({"Close": [100 + i for i in range(30)]})
    monkeypatch.setattr(ub, "_download_nifty_cached", lambda *a, **k: nifty)

    details = ub.build_dynamic_universe_details(target_size=2)

    assert details["scored"] >= 2
    assert details["selected"]
    assert details["scoring_mode"] in {"FULL", "PARTIAL", "TREND_ONLY"}


def test_dynamic_universe_trend_only_fallback_when_few_scored(monkeypatch):
    monkeypatch.setattr(ub, "load_nifty100_symbols", lambda: ["AAA", "BBB", "CCC", "DDD"])
    monkeypatch.setattr(ub, "load_excluded", lambda: [])
    monkeypatch.setattr(ub, "save_universe", lambda symbols, path=None: path or "x")

    # Create only trend-valid data, but no usable nifty rs input.
    parts = []
    for tk in ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS"]:
        sdf = _mk_sdf(candles=210)
        sdf.columns = pd.MultiIndex.from_product([[tk], sdf.columns])
        parts.append(sdf)
    hist = pd.concat(parts, axis=1)

    monkeypatch.setattr(ub, "_download_cached", lambda *a, **k: hist)
    monkeypatch.setattr(ub, "_download_nifty_cached", lambda *a, **k: pd.DataFrame())

    details = ub.build_dynamic_universe_details(target_size=3)

    assert details["selected"]
    assert details["scoring_mode"] == "TREND_ONLY"
