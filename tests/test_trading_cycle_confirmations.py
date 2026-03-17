import os
import sys

import pandas as pd

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def test_confirm_long_htf_with_optional_rsi_gate(monkeypatch):
    df = pd.DataFrame({"close": [100 + i for i in range(40)]})
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_RSI", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_LONG_MIN_RSI", 95.0, raising=False)

    assert tc.confirm_long_htf("RELIANCE", regime="TRENDING") is False



def test_confirm_short_htf_with_optional_rsi_gate(monkeypatch):
    df = pd.DataFrame({"close": [200 - i for i in range(40)]})
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_RSI", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_SHORT_MAX_RSI", -1.0, raising=False)

    assert tc.confirm_short_htf("RELIANCE") is False



def test_generate_short_signal_uses_relative_weakness(monkeypatch):
    monkeypatch.setattr(
        tc,
        "_quality_metrics",
        lambda _sym: {
            "ok": True,
            "price": 90.0,
            "sma20": 100.0,
            "sma20_prev": 101.0,
            "vol_score": 1.6,
            "rs_vs_nifty": 0.4,
        },
    )
    monkeypatch.setattr(tc.CFG, "SHORT_MIN_VOLUME_SCORE", 1.2, raising=False)
    monkeypatch.setattr(tc.CFG, "SHORT_RS_MAX_VS_NIFTY", -0.2, raising=False)

    assert tc.generate_short_signal("RELIANCE") is None



def test_build_fallback_universe_prefers_scored_symbols(monkeypatch):
    monkeypatch.setattr(tc, "load_universe_live", lambda: ["A", "B", "C"])
    monkeypatch.setattr(tc, "load_universe_trading", lambda: [])
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "FALLBACK_TOP_N", 2, raising=False)

    scores = {"A": 0.2, "B": 0.9, "C": 0.5}
    monkeypatch.setattr(tc, "_fallback_candidate_score", lambda sym: scores.get(sym))

    out = tc.build_fallback_universe()

    assert out[:3] == ["B", "C", "A"]


def test_confirm_long_htf_sideways_allows_partial(monkeypatch):
    import pandas as pd

    df = pd.DataFrame({"close": [100.0] * 25 + [101.0] * 15})
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "_session_bucket", lambda *_a, **_k: "LATE")
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)

    assert tc.confirm_long_htf("RELIANCE", regime="SIDEWAYS") is True


def test_confirm_long_htf_volatile_requires_volume_surge(monkeypatch):
    import pandas as pd

    logs = []
    df = pd.DataFrame({"close": [100 + i for i in range(40)]})
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: logs.append(" ".join(str(x) for x in a)))
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)
    monkeypatch.setattr(tc.CFG, "VOLATILE_HTF_MIN_VOL_SCORE", 1.2, raising=False)
    monkeypatch.setattr(tc, "_htf_volume_surge_score", lambda _s: 1.0)

    assert tc.confirm_long_htf("RELIANCE", regime="VOLATILE") is False
    assert any("HTF_Score_Below_Req_for_VOLATILE" in x for x in logs)
