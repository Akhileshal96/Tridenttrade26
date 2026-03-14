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


def test_fill_missed_opportunity_moves_updates_missing_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "SKIPPED_SIGNALS_PATH", str(tmp_path / "skipped_signals.csv"), raising=False)
    t = sa._today_str() + "T10:00:00+05:30"
    sa.record_skipped_signal(
        {
            "time": t,
            "symbol": "ABC",
            "side": "BUY",
            "reason": "market_weak",
            "signal_price": 100,
        }
    )

    def _fetch(_sym, _dt, horizons):
        return {h: (102.0 if h == 15 else 99.0) for h in horizons}

    updated = sa.fill_missed_opportunity_moves(fetcher=_fetch)
    assert updated >= 1
    rows = sa._read_csv_rows(sa.SKIPPED_SIGNALS_PATH)
    assert rows and rows[0].get("after_15m_pct") not in (None, "")


def test_eod_report_includes_filter_effectiveness(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "TRADE_HISTORY_PATH", str(tmp_path / "trade_history.csv"), raising=False)
    monkeypatch.setattr(sa, "STRATEGY_STATS_PATH", str(tmp_path / "strategy_stats.json"), raising=False)
    monkeypatch.setattr(sa, "SKIPPED_SIGNALS_PATH", str(tmp_path / "skipped_signals.csv"), raising=False)
    monkeypatch.setattr(sa, "fill_missed_opportunity_moves", lambda *a, **k: 0)

    today = sa._today_str()
    sa.record_trade_exit(
        {
            "entry_time": today + "T09:30:00+05:30",
            "exit_time": today + "T10:00:00+05:30",
            "symbol": "XYZ",
            "side": "BUY",
            "qty": 1,
            "entry": 100,
            "exit": 105,
            "pnl_inr": 5,
            "pnl_pct": 5,
            "strategy_tag": "primary_long",
            "market_regime": "TRENDING",
            "universe_source": "primary",
            "sector": "IT",
        }
    )
    sa.record_skipped_signal(
        {
            "time": today + "T11:00:00+05:30",
            "symbol": "ABC",
            "side": "BUY",
            "reason": "score_too_low",
            "signal_price": 100,
            "after_15m_pct": 0.6,
            "after_30m_pct": 1.0,
            "after_60m_pct": 0.4,
        }
    )

    txt = sa.generate_eod_report_text({})
    assert "Filter Effectiveness" in txt
    assert "score_too_low" in txt
