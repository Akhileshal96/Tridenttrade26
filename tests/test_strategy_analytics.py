import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import strategy_analytics as sa


def test_record_trade_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "TRADE_HISTORY_PATH", str(tmp_path / "trade_history.csv"), raising=False)
    monkeypatch.setattr(sa, "STRATEGY_STATS_PATH", str(tmp_path / "strategy_stats.json"), raising=False)

    sa.record_trade_exit({
        "entry_time": "2026-01-01T10:00:00+05:30",
        "exit_time": "2026-01-01T10:30:00+05:30",
        "symbol": "ABC",
        "side": "BUY",
        "qty": 2,
        "entry": 100,
        "exit": 110,
        "pnl_inr": 20,
        "pnl_pct": 10,
        "strategy_tag": "primary_long",
        "market_regime": "TRENDING",
        "universe_source": "primary",
        "sector": "IT",
    })
    stats = sa.rebuild_strategy_stats()
    assert stats["strategy"]["primary_long"]["trades"] == 1
    txt = sa.strategy_report_text()
    assert "primary_long" in txt


def test_multiplier_with_insufficient_history(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "TRADE_HISTORY_PATH", str(tmp_path / "trade_history.csv"), raising=False)

    class Cfg:
        MIN_TRADES_FOR_ALLOCATION = 20
        EXPECTANCY_FULL_SIZE = 50
        EXPECTANCY_HALF_SIZE = 10
        DISABLE_NEGATIVE_LAST_N = 10
        USE_OPTIMAL_F = False

    m, r = sa.get_strategy_multiplier("primary_long", Cfg)
    assert m == 1.0
    assert r == "insufficient_history"
