import time
import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def generate_signal(universe):
    """
    Signal: last close > SMA20 (15m candles). Logs real errors.
    """
    kite = get_kite()

    for sym in universe:
        sym = sym.strip().upper()
        if not sym:
            continue

        try:
            token = token_for_symbol(sym)

            # 10 days of 15m candles
            data = kite.historical_data(
                token,
                pd.Timestamp.now() - pd.Timedelta(days=10),
                pd.Timestamp.now(),
                "15minute",
            )

            # Zerodha historical endpoint rate: keep it safe
            time.sleep(0.5)

            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                append_log("WARN", "SIG", f"{sym} no candle data")
                continue

            # Need at least 20 candles for SMA20
            if len(df) < 25:
                append_log("WARN", "SIG", f"{sym} insufficient candles: {len(df)}")
                continue

            sma20 = df["close"].rolling(20).mean()
            avg = float(sma20.iloc[-1]) if pd.notna(sma20.iloc[-1]) else None
            last = float(df["close"].iloc[-1])

            if avg is None or avg <= 0:
                append_log("WARN", "SIG", f"{sym} invalid SMA20={avg}")
                continue

            if last <= 0:
                append_log("WARN", "SIG", f"{sym} invalid last close={last}")
                continue

            # Near-signal visibility (within 0.2%)
            if abs(last - avg) / avg < 0.002:
                append_log("INFO", "NEAR", f"{sym} near: last={last:.2f} sma20={avg:.2f}")

            if last > avg:
                append_log("INFO", "SIG", f"{sym} BUY trigger last={last:.2f} sma20={avg:.2f}")
                return {"symbol": sym, "side": "BUY", "entry": float(last)}

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No signal found")
    return None
