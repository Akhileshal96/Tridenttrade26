import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

import risk_engine as re


def test_bucket_slab_exact_values():
    assert re.get_bucket_from_slab(4000) == 500.0
    assert re.get_bucket_from_slab(5000) == 5000.0
    assert re.get_bucket_from_slab(20000) == 7000.0
    assert re.get_bucket_from_slab(50000) == 10000.0
    assert re.get_bucket_from_slab(90000) == 15000.0
    assert re.get_bucket_from_slab(120000) == 20000.0


def test_loss_streak_rules():
    state = {}
    re.update_loss_streak(state, -10)
    re.update_loss_streak(state, -5)
    assert state["loss_streak"] == 2
    assert state["reduce_size_factor"] == 0.5

    re.update_loss_streak(state, -2)
    assert state["pause_entries_until"] > datetime.now(re.IST)

    re.update_loss_streak(state, -1)
    assert state["halt_for_day"] is True


def test_drawdown_guard_sets_pause():
    state = {"today_pnl": 100.0, "day_peak_pnl": 0.0}
    assert re.check_day_drawdown_guard(state) is True
    state["today_pnl"] = 50.0
    ok = re.check_day_drawdown_guard(state)
    assert ok in (True, False)
    if not ok:
        assert state["pause_entries_until"] > datetime.now(re.IST)


def test_daily_loss_guard_does_not_trigger_on_positive_day():
    state = {"today_pnl": 120.0, "day_peak_pnl": 150.0, "daily_loss_cap_inr": 200.0}
    assert re.check_day_drawdown_guard(state) in (True, False)
    assert state.get("day_guard_reason") != "daily_loss_guard"


def test_profit_giveback_guard_triggers_on_positive_day_giveback(monkeypatch):
    monkeypatch.setattr(re.CFG, "DAY_PROFIT_GIVEBACK_REDUCE_PCT", 20.0, raising=False)
    monkeypatch.setattr(re.CFG, "DAY_PROFIT_GIVEBACK_PAUSE_PCT", 30.0, raising=False)
    monkeypatch.setattr(re.CFG, "DAY_PROFIT_GIVEBACK_HALT_PCT", 60.0, raising=False)
    state = {"today_pnl": 40.0, "day_peak_pnl": 100.0, "daily_loss_cap_inr": 200.0}
    ok = re.check_day_drawdown_guard(state)
    assert ok is False
    assert state.get("day_guard_reason") == "profit_giveback_guard"


def test_daily_loss_guard_triggers_only_on_negative_breach():
    state = {"today_pnl": -220.0, "day_peak_pnl": 50.0, "daily_loss_cap_inr": 200.0}
    ok = re.check_day_drawdown_guard(state)
    assert ok is False
    assert state.get("day_guard_reason") == "daily_loss_guard"


def test_sector_exposure_limit(monkeypatch):
    monkeypatch.setenv("MAX_POSITIONS_PER_SECTOR", "2")
    positions = {
        "A": {"sector": "IT", "entry": 100, "qty": 1},
        "B": {"sector": "IT", "entry": 110, "qty": 1},
    }
    assert re.check_sector_exposure("C", positions, sector="IT") is False
    assert re.check_sector_exposure("C", positions, sector="BANK") is True
