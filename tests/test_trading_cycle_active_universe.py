import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import trading_cycle as tc

IST = ZoneInfo("Asia/Kolkata")


def test_confirm_long_htf_early_session_relaxed(monkeypatch):
    import pandas as pd

    # Price above MA while MA is not rising; should pass only in EARLY bucket.
    df = pd.DataFrame(
        {
            "close": [
                86.17, 104.27, 115.34, 102.08, 108.79, 108.40, 111.85, 95.43, 86.74, 114.93,
                119.17, 99.12, 98.31, 84.03, 106.40, 111.26, 115.84, 84.62, 119.44, 94.60,
                99.74, 117.50, 103.97, 89.07, 91.67, 118.64, 85.81, 87.89, 96.64, 114.24,
            ]
        }
    )
    monkeypatch.setattr(tc, "_htf_fetch", lambda *_a, **_k: df)
    monkeypatch.setattr(tc, "_session_bucket", lambda *_a, **_k: "EARLY")
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc.CFG, "USE_MTF_CONFIRMATION", True, raising=False)
    monkeypatch.setattr(tc.CFG, "HTF_CONFIRM_MA", 20, raising=False)

    assert tc.confirm_long_htf("ABC") is True


def test_build_active_universe_uses_scored_ranking(monkeypatch):
    monkeypatch.setattr(tc.CFG, "ACTIVE_UNIVERSE_SIZE", 2, raising=False)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_sector_strength_snapshot", lambda _u: {"OTHER": 1.0})

    scores = {"A": {"ok": True, "score": 1.0}, "B": {"ok": True, "score": 3.0}, "C": {"ok": True, "score": 2.0}}
    monkeypatch.setattr(tc, "_active_score_metrics", lambda sym, _ss: scores[sym])

    out = tc.build_active_universe(["A", "B", "C"])
    assert out == ["B", "C"]


def test_refresh_active_universe_if_due(monkeypatch):
    monkeypatch.setattr(tc.CFG, "ACTIVE_UNIVERSE_REFRESH_MINUTES", 10, raising=False)
    monkeypatch.setattr(tc, "build_active_universe", lambda u: ["X", "Y"])

    tc.STATE["active_universe"] = ["A"]
    tc.STATE["active_universe_last_refresh"] = datetime.now(IST) - timedelta(minutes=11)

    out = tc.refresh_active_universe_if_due(["A", "B"])
    assert out == ["X", "Y"]


def test_opening_mode_unsafe_from_metrics(monkeypatch):
    monkeypatch.setattr(tc.CFG, "USE_ADAPTIVE_OPEN_FILTER", True, raising=False)
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tc,
        "_compute_opening_metrics",
        lambda: {
            "gap_pct": 1.5,
            "first_5m_range_pct": 2.0,
            "direction_clear": False,
            "spread_quality": "WIDE",
            "volume_quality": "LOW",
            "valid": True,
        },
    )
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    mode, _m = tc.get_opening_mode()
    assert mode == "OPEN_UNSAFE"


def test_tick_blocks_entries_on_open_unsafe(monkeypatch):
    tc.STATE["paused"] = False
    tc.STATE["positions"] = {}
    tc.STATE["fallback_mode_active"] = False
    tc.STATE["no_entry_cycles"] = 0

    monkeypatch.setattr(tc, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(tc.RISK, "sync_wallet", lambda _s: None)
    monkeypatch.setattr(tc, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(tc.RISK, "check_day_drawdown_guard", lambda _s: False)
    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "_within_entry_window", lambda: True)
    monkeypatch.setattr(tc, "_resolve_trade_universe", lambda: ["A", "B", "C"])
    monkeypatch.setattr(tc, "refresh_active_universe_if_due", lambda _u: ["A", "B"])
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "TRENDING"})
    monkeypatch.setattr(tc, "get_regime_entry_mode", lambda _r: "LONG")
    monkeypatch.setattr(tc, "get_opening_mode", lambda: ("OPEN_UNSAFE", {}))
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda *_a, **_k: True)
    monkeypatch.setattr(tc, "ee_monitor_positions", lambda *a, **k: None)

    called = {"long": 0, "short": 0}
    monkeypatch.setattr(tc, "_scan_long_entries", lambda *a, **k: called.__setitem__("long", called["long"] + 1) or 0)
    monkeypatch.setattr(tc, "_scan_short_entries", lambda *a, **k: called.__setitem__("short", called["short"] + 1) or 0)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    tc.tick()

    assert called["long"] == 0
    assert called["short"] == 0
