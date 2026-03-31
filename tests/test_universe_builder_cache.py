import os
import sys

sys.path.insert(0, os.getcwd())

import pandas as pd
import universe_builder as ub


def test_download_cached_hits_cache(monkeypatch):
    ub._DOWNLOAD_CACHE.clear()
    monkeypatch.setattr(ub.CFG, "UNIVERSE_CACHE_TTL_SEC", 600, raising=False)

    calls = {"n": 0}

    def fake_download(symbols, period="6mo", interval="1d"):
        calls["n"] += 1
        cols = pd.MultiIndex.from_product([["ABC.NS"], ["Close"]])
        return pd.DataFrame([[1.0]], columns=cols)

    monkeypatch.setattr(ub, "_download", fake_download)
    monkeypatch.setattr(ub, "append_log", lambda *args, **kwargs: None)

    a = ub._download_cached(["ABC"], period="6mo", interval="1d")
    b = ub._download_cached(["ABC"], period="6mo", interval="1d")

    assert not a.empty and not b.empty
    assert calls["n"] == 1
