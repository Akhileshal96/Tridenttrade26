import time
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log

ALLOWED_INTERVALS = {
    "minute", "day",
    "3minute", "5minute", "10minute",
    "15minute", "30minute", "60minute",
}

def _validated_interval() -> str:
    iv = (CFG.HIST_INTERVAL or "15minute").strip()
    if iv not in ALLOWED_INTERVALS:
        append_log("WARN", "HIST", f"Invalid HIST_INTERVAL='{iv}'. Falling back to '15minute'.")
        return "15minute"
    return iv

def _now_ist_naive() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)

def generate_signal(universe: list[str]) -> dict | None:
    kite = get_kite()
    interval = _validated_interval()

    end = _now_ist_naive()
    start = end - timedelta(days=max(1, int(CFG.HIST_DAYS or 10)))

    for sym in universe:
        try:
            token = token_for_symbol(sym)

            data = kite.historical_data(token, start, end, interval)
            time.sleep(0.40)  # stay under rate limits

            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                continue

            df["ma20"] = df["close"].rolling(20).mean()
            last = float(df["close"].iloc[-1])
            ma20 = df["ma20"].iloc[-1]

            if pd.notna(ma20) and last > float(ma20):
                return {"symbol": sym, "side": "BUY", "entry_ref": last}

        except Exception:
            continue

    append_log("INFO", "SIG", "No signal found")
    return None
