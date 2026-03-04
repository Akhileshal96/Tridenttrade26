import excluded_store
import time
import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def _now():
    return pd.Timestamp.now(tz="Asia/Kolkata")


def generate_signal(universe):
    """
    Simple signal:
    - Fetch last 10 days of 15m candles
    - BUY if last close > SMA20
    Observability:
    - Logs each scan
    - Logs exact exception per symbol
    """
    kite = get_kite()

    for sym in universe:
        # insider safety: skip excluded symbols
        if str(sym).strip().upper() in excluded_store.load_excluded():
            continue
        sym = (sym or "").strip().upper()
        if not sym:
            continue

        # Skip if excluded list exists and contains symbol (optional)
        try:
            if hasattr(CFG, "EXCLUDE_PATH") and CFG.EXCLUDE_PATH:
                import os
                if os.path.exists(CFG.EXCLUDE_PATH):
                    with open(CFG.EXCLUDE_PATH, "r") as f:
                        blocked = {x.strip().upper() for x in f.read().splitlines() if x.strip()}
                    if sym in blocked:
                        append_log("INFO", "SCAN", f"Skipping excluded {sym}")
                        continue
        except Exception as e:
            append_log("WARN", "SCAN", f"Exclude check failed: {e}")

        append_log("INFO", "SCAN", f"Scanning {sym}")

        try:
            token = token_for_symbol(sym)

            # 10 days of candles (rate-limit friendly)
            frm = _now() - pd.Timedelta(days=10)
            to = _now()
            data = kite.historical_data(token, frm.to_pydatetime(), to.to_pydatetime(), "15minute")

            time.sleep(0.45)  # keep under 3 req/sec

            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                append_log("WARN", "SIG", f"{sym} no candle data")
                continue

            # Need >= 20 candles for SMA20
            if len(df) < 25:
                append_log("WARN", "SIG", f"{sym} insufficient candles={len(df)}")
                continue

            closes = df["close"].astype(float)
            sma20 = closes.rolling(20).mean().iloc[-1]
            last = float(closes.iloc[-1])

            if pd.isna(sma20) or sma20 <= 0:
                append_log("WARN", "SIG", f"{sym} SMA20 invalid")
                continue

            # Near-signal visibility (within 0.2%)
            if abs(last - sma20) / sma20 < 0.002:
                append_log("INFO", "NEAR", f"{sym} near: last={last:.2f} sma20={sma20:.2f}")

            if last > sma20:
                append_log("INFO", "SIG", f"{sym} BUY trigger last={last:.2f} sma20={sma20:.2f}")
                return {"symbol": sym, "side": "BUY", "entry": last}

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", f"No signal | score={score} threshold={threshold} rsi={rsi} ema20={ema20} ema50={ema50} macd={macd}")
    return None
