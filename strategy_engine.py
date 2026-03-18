import time
import pandas as pd

from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def _compute_entry_buffer(df: pd.DataFrame, last: float, sma20: float) -> float:
    fixed_pct = float(getattr(CFG, "SMA20_ENTRY_BUFFER_PCT", 0.1) or 0.1) / 100.0
    pct_buffer = max(0.0, sma20 * fixed_pct)

    atr_mult = float(getattr(CFG, "SMA20_ENTRY_BUFFER_ATR_MULT", 0.0) or 0.0)
    atr_buffer = 0.0
    if atr_mult > 0 and all(c in df.columns for c in ("high", "low", "close")) and len(df) >= 20:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        if not pd.isna(atr):
            atr_buffer = float(atr) * atr_mult

    return max(pct_buffer, atr_buffer)


def generate_signal(universe):
    """
    Signal: last close > SMA20 (15m candles). Logs real errors.
    """
    kite = get_kite()

    for sym in universe:
        sym = (sym or "").strip().upper()
        if not sym:
            continue

        try:
            token = token_for_symbol(sym)

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

            if len(df) < 25:
                append_log("WARN", "SIG", f"{sym} insufficient candles: {len(df)}")
                continue

            sma20 = df["close"].rolling(20).mean()
            last = float(df["close"].iloc[-1])
            avg_val = sma20.iloc[-1]
            if pd.isna(avg_val):
                append_log("WARN", "SIG", f"{sym} SMA20 NA")
                continue

            avg = float(avg_val)

            if last <= 0 or avg <= 0:
                append_log("WARN", "SIG", f"{sym} invalid prices last={last} sma20={avg}")
                continue

            buffer = _compute_entry_buffer(df, last, avg)
            trigger = avg + buffer
            append_log(
                "INFO",
                "SIG",
                f"{sym} primary_eval_ok last={last:.2f} sma20={avg:.2f} buffer={buffer:.4f} trigger={trigger:.2f}",
            )

            # Strict trigger: do not spam NEAR logs or emit attempts unless buffer is cleared.
            if last <= trigger:
                continue

            if last > trigger:
                append_log(
                    "INFO",
                    "SIG",
                    f"{sym} BUY trigger last={last:.2f} sma20={avg:.2f} buffer={buffer:.4f} trigger={trigger:.2f}",
                )
                return {"symbol": sym, "side": "BUY", "entry": float(last), "sma20": avg, "entry_buffer": buffer}

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No signal found")
    return None


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def generate_mean_reversion_signal(universe):
    """Fallback signal: RSI oversold rebound or lower-BB bounce."""
    kite = get_kite()

    for sym in universe:
        sym = (sym or "").strip().upper()
        if not sym:
            continue

        try:
            token = token_for_symbol(sym)
            data = kite.historical_data(
                token,
                pd.Timestamp.now() - pd.Timedelta(days=10),
                pd.Timestamp.now(),
                "15minute",
            )
            time.sleep(0.5)
            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns or len(df) < 25:
                continue

            close = df["close"].astype(float)
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            if pd.isna(sma20.iloc[-1]) or pd.isna(std20.iloc[-1]):
                continue

            lower = float(sma20.iloc[-1] - (2.0 * std20.iloc[-1]))
            prev_lower = float(sma20.iloc[-2] - (2.0 * std20.iloc[-2])) if not pd.isna(std20.iloc[-2]) else lower
            rsi = _calc_rsi(close)
            rsi_last = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

            # Mean-reversion long setups: RSI oversold or lower-band bounce confirmation.
            rsi_setup = rsi_last < 30.0
            bb_bounce_setup = prev <= prev_lower and last > lower
            if not (rsi_setup or bb_bounce_setup):
                continue

            append_log(
                "INFO",
                "SIG",
                f"{sym} MR BUY trigger last={last:.2f} rsi={rsi_last:.2f} lower_bb={lower:.2f} setup={'RSI' if rsi_setup else 'BB_BOUNCE'}",
            )
            return {
                "symbol": sym,
                "side": "BUY",
                "entry": last,
                "strategy_setup": "mean_reversion",
                "rsi": rsi_last,
                "lower_bb": lower,
            }

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No mean-reversion signal found")
    return None
