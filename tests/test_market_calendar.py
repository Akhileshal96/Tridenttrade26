"""Tests for market_calendar.py — NSE holiday/weekend detection."""
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import market_calendar as MCAL

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Helpers
# ============================================================================

def _write_holidays(tmp_path, entries):
    """Write a JSON file in flat-list format and point MCAL at it."""
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    os.environ["MARKET_HOLIDAYS_FILE"] = str(p)
    MCAL.reload_holidays()
    return p


def _clear_env():
    os.environ.pop("MARKET_HOLIDAYS_FILE", None)


# ============================================================================
# is_weekend
# ============================================================================

def test_saturday_is_weekend():
    assert MCAL.is_weekend(date(2026, 5, 2)) is True   # Saturday


def test_sunday_is_weekend():
    assert MCAL.is_weekend(date(2026, 5, 3)) is True   # Sunday


def test_friday_is_not_weekend():
    assert MCAL.is_weekend(date(2026, 5, 1)) is False  # Friday (but a holiday)


def test_monday_is_not_weekend():
    assert MCAL.is_weekend(date(2026, 5, 4)) is False  # Monday


# ============================================================================
# is_market_holiday
# ============================================================================

def test_holiday_recognized_from_seed_file():
    # Seed file ships with Maharashtra Day on 2026-05-01
    h, name = MCAL.is_market_holiday(date(2026, 5, 1))
    assert h is True
    assert "Maharashtra" in name


def test_non_holiday_returns_false():
    h, name = MCAL.is_market_holiday(date(2026, 5, 4))  # regular Monday
    assert h is False
    assert name == ""


def test_holiday_loaded_from_custom_file(tmp_path):
    _write_holidays(tmp_path, [
        {"date": "2026-06-15", "name": "Test Holiday"}
    ])
    try:
        h, name = MCAL.is_market_holiday(date(2026, 6, 15))
        assert h is True
        assert name == "Test Holiday"
    finally:
        _clear_env()
        MCAL.reload_holidays()


def test_loader_handles_dict_format(tmp_path):
    """Format B: flat dict {YYYY-MM-DD: name}"""
    p = tmp_path / "holidays.json"
    p.write_text(json.dumps({"2026-06-15": "Dict Holiday"}), encoding="utf-8")
    os.environ["MARKET_HOLIDAYS_FILE"] = str(p)
    try:
        MCAL.reload_holidays()
        h, name = MCAL.is_market_holiday(date(2026, 6, 15))
        assert h is True
        assert name == "Dict Holiday"
    finally:
        _clear_env()
        MCAL.reload_holidays()


def test_loader_handles_nested_year_format(tmp_path):
    """Format C: {YYYY: {YYYY-MM-DD: name}}"""
    p = tmp_path / "holidays.json"
    p.write_text(
        json.dumps({"2026": {"2026-06-15": "Nested Holiday"}}),
        encoding="utf-8",
    )
    os.environ["MARKET_HOLIDAYS_FILE"] = str(p)
    try:
        MCAL.reload_holidays()
        h, name = MCAL.is_market_holiday(date(2026, 6, 15))
        assert h is True
        assert name == "Nested Holiday"
    finally:
        _clear_env()
        MCAL.reload_holidays()


def test_loader_fails_open_on_missing_file(tmp_path, monkeypatch):
    """Missing file → no holidays loaded, all weekdays = trading days (fail-open)."""
    missing = tmp_path / "does_not_exist.json"
    os.environ["MARKET_HOLIDAYS_FILE"] = str(missing)
    try:
        n = MCAL.reload_holidays()
        assert n == 0
        # 2026-05-01 in seed is a holiday but seed isn't loaded now
        h, _ = MCAL.is_market_holiday(date(2026, 5, 1))
        assert h is False
    finally:
        _clear_env()
        MCAL.reload_holidays()


def test_loader_fails_open_on_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json }", encoding="utf-8")
    os.environ["MARKET_HOLIDAYS_FILE"] = str(p)
    try:
        n = MCAL.reload_holidays()
        # Bad JSON → 0 entries, no exceptions
        assert n == 0
    finally:
        _clear_env()
        MCAL.reload_holidays()


# ============================================================================
# is_trading_day
# ============================================================================

def test_holiday_is_not_trading_day():
    assert MCAL.is_trading_day(date(2026, 5, 1)) is False  # Maharashtra Day


def test_saturday_is_not_trading_day():
    assert MCAL.is_trading_day(date(2026, 5, 2)) is False


def test_sunday_is_not_trading_day():
    assert MCAL.is_trading_day(date(2026, 5, 3)) is False


def test_regular_monday_is_trading_day():
    assert MCAL.is_trading_day(date(2026, 5, 4)) is True


# ============================================================================
# next_trading_day
# ============================================================================

def test_next_trading_day_skips_weekend_and_holiday():
    # Friday 2026-05-01 is Maharashtra Day → next trading day is Mon 2026-05-04
    nxt = MCAL.next_trading_day(date(2026, 5, 1))
    assert nxt == date(2026, 5, 4)


def test_next_trading_day_from_friday():
    # Regular Friday 2026-05-08 → next trading day is Monday 2026-05-11
    nxt = MCAL.next_trading_day(date(2026, 5, 8))
    assert nxt == date(2026, 5, 11)


def test_next_trading_day_from_saturday():
    # Saturday 2026-05-02 → next trading day is Monday 2026-05-04
    nxt = MCAL.next_trading_day(date(2026, 5, 2))
    assert nxt == date(2026, 5, 4)


def test_next_trading_day_from_sunday():
    nxt = MCAL.next_trading_day(date(2026, 5, 3))
    assert nxt == date(2026, 5, 4)


# ============================================================================
# Diagnostic snapshot
# ============================================================================

def test_calendar_status_returns_expected_keys():
    s = MCAL.calendar_status()
    expected_keys = {
        "loaded_from", "entries", "today", "is_weekend",
        "is_holiday", "holiday_name", "is_trading_day", "next_trading_day"
    }
    assert expected_keys.issubset(s.keys())


def test_holiday_name_today_is_none_or_string():
    name = MCAL.holiday_name_today()
    assert name is None or isinstance(name, str)
