import time
from datetime import datetime

import pandas as pd

from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def _in_early_session() -> bool:
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t < (9 * 60 + 30)


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _htf_confirm(kite, token, last_price: float) -> bool:
    htf_data = kite.historical_data(
        token,
        pd.Timestamp.now() - pd.Timedelta(days=20),
        pd.Timestamp.now(),
        "60minute",
    )
    time.sleep(0.2)
    htf_df = pd.DataFrame(htf_data)
    if htf_df.empty or "close" not in htf_df.columns or len(htf_df) < 25:
        return False

    htf_sma20 = htf_df["close"].rolling(20).mean()
    sma = _safe_float(htf_sma20.iloc[-1], default=0.0)
    prev = _safe_float(htf_sma20.iloc[-2], default=sma)
    if sma <= 0:
        return False

    if _in_early_session():
        ok = last_price > sma
        if ok:
            append_log("INFO", "CONFIRM", f"early_session_relaxed_htf price={last_price:.2f} htf_sma20={sma:.2f}")
        return ok

    slope_up = sma > prev
    return (last_price > sma) and slope_up


def _build_buy_signal(sym: str, df: pd.DataFrame):
    sma20 = df["close"].rolling(20).mean()
    last = float(df["close"].iloc[-1])
    avg = _safe_float(sma20.iloc[-1], default=0.0)
    if avg <= 0 or last <= 0:
        return None
    if abs(last - avg) / avg < 0.002:
        append_log("INFO", "NEAR", f"{sym} near: last={last:.2f} sma20={avg:.2f}")
    if last > avg:
        return {"symbol": sym, "side": "BUY", "entry": last, "reason": "sma20_break"}
    return None


def _build_short_signal(sym: str, df: pd.DataFrame):
    if len(df) < 30 or not all(c in df.columns for c in ["close", "volume"]):
        return None

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    sma20 = close.rolling(20).mean()
    rsi14 = _rsi(close, 14)

    last = _safe_float(close.iloc[-1])
    avg = _safe_float(sma20.iloc[-1])
    rsi = _safe_float(rsi14.iloc[-1], default=50.0)
    momentum = _safe_float(close.iloc[-1] - close.iloc[-5])
    vol_ratio = _safe_float(volume.iloc[-1] / max(1.0, volume.tail(20).mean()), default=0.0)

    if last < avg and rsi < 45 and momentum < 0 and vol_ratio >= 1.1:
        append_log(
            "INFO",
            "TRADE",
            f"SHORT ENTRY {sym} last={last:.2f} sma20={avg:.2f} rsi={rsi:.1f} mom={momentum:.2f} volx={vol_ratio:.2f}",
        )
        return {"symbol": sym, "side": "SELL", "entry": last, "reason": "weak_regime_short"}

    return None


def generate_signal(universe, allow_short=False):
    """
    BUY signal: last close > SMA20 with HTF confirmation.
    Optional SELL signal: weak regime short mode.
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
            time.sleep(0.5)

            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                append_log("WARN", "SIG", f"{sym} no candle data")
                continue
            if len(df) < 25:
                append_log("WARN", "SIG", f"{sym} insufficient candles: {len(df)}")
                continue

            if allow_short:
                s_sig = _build_short_signal(sym, df)
                if s_sig:
                    return s_sig

            b_sig = _build_buy_signal(sym, df)
            if not b_sig:
                continue

            if _htf_confirm(kite, token, float(b_sig["entry"])):
                append_log("INFO", "SIG", f"{sym} BUY trigger last={b_sig['entry']:.2f} + HTF confirm")
                return b_sig

            append_log("INFO", "SIG", f"{sym} BUY rejected: HTF confirm failed")

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No signal found")
    return None
