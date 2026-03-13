import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def read_symbols(path: str, limit: int | None = None):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        syms = [ln.strip().upper() for ln in f if ln.strip()]
    return syms[:limit] if limit else syms


def should_refresh(last_refresh, interval_min: int = 10) -> bool:
    if not last_refresh:
        return True
    return (datetime.now(IST) - last_refresh) >= timedelta(minutes=interval_min)
