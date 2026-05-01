"""NSE/BSE trading-day calendar.

Determines whether a given date is a trading day (a weekday that is not on
the holiday list) for Indian equity markets. Used by the trading loop to
short-circuit signal generation, scanning, and order placement on holidays
and weekends.

Holiday data is loaded from `data/nse_holidays.json` (one-time read at import,
re-readable via `reload_holidays()`). The bundled file is a starter set —
you should update it from the official NSE annual holiday list each year.

Public API
----------
  is_market_holiday(date) -> (bool, str)   # (is_holiday, name_or_empty)
  is_weekend(date) -> bool
  is_trading_day(date) -> bool             # weekday AND not on holiday list
  next_trading_day(from_date) -> date      # skips weekends + holidays
  holiday_name_today() -> str | None       # convenience for today
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")

_HOLIDAYS_FILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "nse_holidays.json"
)

# Hardcoded fallback so the bot works correctly even without an external JSON
# file (the data/ directory is .gitignore'd, so deploys don't carry the file).
# Only fixed-date NSE holidays — variable-date ones (Holi, Eid, Diwali, Good
# Friday, Mahavir Jayanti, Ganesh Chaturthi, etc.) MUST be added via the JSON
# file each year from the official NSE annual holiday list.
_DEFAULT_HOLIDAYS: dict[str, str] = {
    "2026-01-26": "Republic Day",
    "2026-05-01": "Maharashtra Day",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-12-25": "Christmas",
    "2027-01-26": "Republic Day",
    "2027-05-01": "Maharashtra Day",
    "2027-08-15": "Independence Day",
    "2027-10-02": "Mahatma Gandhi Jayanti",
    "2027-12-25": "Christmas",
}

# In-memory cache: { "YYYY-MM-DD": "Holiday Name" }
_HOLIDAYS: dict[str, str] = {}
_HOLIDAYS_LOADED_FROM: str | None = None


def _holidays_file_path() -> str:
    """Allow runtime override via env (useful for tests / multi-region setups)."""
    return os.environ.get("MARKET_HOLIDAYS_FILE", _HOLIDAYS_FILE_DEFAULT)


def reload_holidays() -> int:
    """(Re-)load the holidays. Always seeds from _DEFAULT_HOLIDAYS first,
    then merges any external JSON file on top (file entries take precedence
    on duplicate dates). Returns the total number of entries loaded.
    """
    global _HOLIDAYS, _HOLIDAYS_LOADED_FROM
    path = _holidays_file_path()
    # Always start with the hardcoded fallback — fixed-date holidays at minimum.
    _HOLIDAYS = dict(_DEFAULT_HOLIDAYS)
    _HOLIDAYS_LOADED_FROM = "builtin_defaults"
    if not os.path.exists(path):
        try:
            append_log("INFO", "CAL", f"loaded {len(_HOLIDAYS)} builtin default holidays (file not found at {os.path.basename(path)})")
        except Exception:
            pass
        return len(_HOLIDAYS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            # Format A: [{"date": "YYYY-MM-DD", "name": "..."}, ...]
            for entry in data:
                d = str(entry.get("date") or "").strip()
                n = str(entry.get("name") or "").strip() or "Holiday"
                if d:
                    _HOLIDAYS[d] = n
        elif isinstance(data, dict):
            # Format B: {"YYYY-MM-DD": "Name", ...}  (flat) OR
            # Format C: {"2026": {"YYYY-MM-DD": "Name"}, ...}  (nested by year)
            for k, v in data.items():
                if isinstance(v, dict):
                    for d, n in v.items():
                        _HOLIDAYS[str(d).strip()] = str(n or "Holiday").strip()
                else:
                    _HOLIDAYS[str(k).strip()] = str(v or "Holiday").strip()
        _HOLIDAYS_LOADED_FROM = path
        try:
            append_log("INFO", "CAL", f"loaded {len(_HOLIDAYS)} holidays (builtin + {os.path.basename(path)})")
        except Exception:
            pass
    except Exception as exc:
        try:
            append_log("ERROR", "CAL", f"failed to load holidays from {path}: {exc} (falling back to {len(_DEFAULT_HOLIDAYS)} builtins)")
        except Exception:
            pass
        # Keep builtins on parse failure — better than zero entries.
        _HOLIDAYS = dict(_DEFAULT_HOLIDAYS)
        _HOLIDAYS_LOADED_FROM = "builtin_defaults"
    return len(_HOLIDAYS)


# Load at import time (safe — fails open if file is missing or malformed).
reload_holidays()


# ============================================================================
# Public predicates
# ============================================================================

def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    raise TypeError(f"expected date or datetime, got {type(d).__name__}")


def is_weekend(d) -> bool:
    """True if the given date is Saturday or Sunday."""
    return _to_date(d).weekday() >= 5


def is_market_holiday(d) -> tuple[bool, str]:
    """Return (is_holiday, holiday_name). Empty string when not a holiday."""
    key = _to_date(d).isoformat()
    name = _HOLIDAYS.get(key, "")
    return (bool(name), name)


def is_trading_day(d) -> bool:
    """True only for non-weekend days that are not on the holiday list."""
    if is_weekend(d):
        return False
    h, _ = is_market_holiday(d)
    return not h


def next_trading_day(from_date=None) -> date:
    """Return the next date (strictly after `from_date`) that is a trading day."""
    cur = _to_date(from_date) if from_date is not None else datetime.now(IST).date()
    for _ in range(1, 366):  # safety upper bound
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            return cur
    # If we somehow exhaust a year, fall through to the original date.
    return cur


def holiday_name_today() -> Optional[str]:
    """Convenience: return holiday name for today (IST), else None."""
    h, name = is_market_holiday(datetime.now(IST).date())
    return name if h else None


def calendar_status() -> dict:
    """Diagnostic snapshot used by /status and tests."""
    today = datetime.now(IST).date()
    h, name = is_market_holiday(today)
    return {
        "loaded_from": _HOLIDAYS_LOADED_FROM,
        "entries": len(_HOLIDAYS),
        "today": today.isoformat(),
        "is_weekend": is_weekend(today),
        "is_holiday": h,
        "holiday_name": name,
        "is_trading_day": is_trading_day(today),
        "next_trading_day": next_trading_day(today).isoformat(),
    }
