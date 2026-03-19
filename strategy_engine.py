import time
import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def _cfg_obj():
    # Defensive local resolver for runtime environments that may hold stale module globals.
    import config as cfg
    return cfg


def _cfg_float(name: str, default: float) -> float:
    raw = getattr(_cfg_obj(), name, None)
    if raw is None:
        append_log("INFO", "CONFIRM", f"using config default for {name}={default}")
        return float(default)
    try:
        return float(raw)
    except Exception:
        append_log("WARN", "CONFIRM", f"invalid config for {name}={raw}; using default={default}")
        return float(default)


def _cfg_int(name: str, default: int) -> int:
    raw = getattr(_cfg_obj(), name, None)
    if raw is None:
        append_log("INFO", "CONFIRM", f"using config default for {name}={default}")
        return int(default)
    try:
        return int(raw)
    except Exception:
        append_log("WARN", "CONFIRM", f"invalid config for {name}={raw}; using default={default}")
        return int(default)


def _compute_entry_buffer(df: pd.DataFrame, last: float, sma20: float, symbol: str = "") -> float:
    fixed_pct = _cfg_float("SMA20_ENTRY_BUFFER_PCT", 0.1) / 100.0
    pct_buffer = max(0.0, sma20 * fixed_pct)

    atr_mult = _cfg_float("SMA20_ENTRY_BUFFER_ATR_MULT", 0.0)
    atr_buffer = 0.0
    if atr_mult > 0:
        if not all(c in df.columns for c in ("high", "low", "close")):
            if symbol:
                append_log("INFO", "SIG", f"{symbol} partial_eval reason=insufficient_history_for_indicator indicator=ATR")
        elif len(df) < 15:
            if symbol:
                append_log("INFO", "SIG", f"{symbol} partial_eval reason=insufficient_history_for_indicator indicator=ATR")
        else:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            prev_close = close.shift(1)
            tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]
            if not pd.isna(atr):
                atr_buffer = float(atr) * atr_mult
            elif symbol:
                append_log("INFO", "SIG", f"{symbol} partial_eval reason=insufficient_history_for_indicator indicator=ATR")

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

            core_min_bars = _cfg_int("SMA20_CORE_MIN_BARS", 20)
            if len(df) < core_min_bars:
                append_log("INFO", "SIG", f"{sym} skipped reason=insufficient_history_min_bars have={len(df)} need={core_min_bars}")
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

            buffer = _compute_entry_buffer(df, last, avg, symbol=sym)
            trigger = avg + buffer
            append_log(
                "INFO",
                "SIG",
                f"primary scan evaluated {sym} successfully last={last:.2f} sma20={avg:.2f} buffer={buffer:.4f} trigger={trigger:.2f}",
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
