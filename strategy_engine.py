import time
import pandas as pd
from zoneinfo import ZoneInfo

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log

LAST_SIGNAL_REJECT_REASONS = {}


def _reject_signal(symbol: str, family: str, reason: str):
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    rej = str(reason or "conditions_not_met")
    LAST_SIGNAL_REJECT_REASONS[sym] = rej
    append_log("INFO", "SIG", f"family={family} symbol={sym} reject={rej}")


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


_ZERODHA_INTERVAL_MAP = {
    "1m": "minute",
    "1min": "minute",
    "minute": "minute",
    "3m": "3minute",
    "3min": "3minute",
    "3minute": "3minute",
    "5m": "5minute",
    "5min": "5minute",
    "5minute": "5minute",
    "10m": "10minute",
    "10min": "10minute",
    "10minute": "10minute",
    "15m": "15minute",
    "15min": "15minute",
    "15minute": "15minute",
    "30m": "30minute",
    "30min": "30minute",
    "30minute": "30minute",
    "60m": "60minute",
    "60min": "60minute",
    "1h": "60minute",
    "60minute": "60minute",
    "1d": "day",
    "day": "day",
}


def normalize_zerodha_interval(interval: str) -> str:
    raw = str(interval or "").strip().lower()
    return _ZERODHA_INTERVAL_MAP.get(raw, raw or "15minute")


def _score_momentum_setup(last: float, trigger: float, rel_vol: float, sma_now: float, sma_prev: float, atr: float) -> float:
    dist_above_trigger = max(0.0, ((last - trigger) / trigger) * 100.0) if trigger > 0 else 0.0
    sma_slope = max(0.0, ((sma_now - sma_prev) / sma_prev) * 100.0) if sma_prev > 0 else 0.0
    atr_norm_dist = max(0.0, (last - sma_now) / atr) if atr > 0 else 0.0
    return (
        (0.40 * dist_above_trigger)
        + (0.30 * max(0.0, rel_vol))
        + (0.20 * max(0.0, sma_slope))
        + (0.10 * max(0.0, atr_norm_dist))
    )


def _score_mean_reversion_setup(rsi_last: float, bounce_size_pct: float, recovery_momentum_pct: float) -> float:
    rsi_depth = max(0.0, 30.0 - float(rsi_last))
    return (
        (0.50 * rsi_depth)
        + (0.30 * max(0.0, bounce_size_pct))
        + (0.20 * max(0.0, recovery_momentum_pct))
    )


def generate_signal(universe):
    """
    Signal: last close > SMA20 (15m candles). Logs real errors.
    """
    kite = get_kite()

    candidates = []
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
                _reject_signal(sym, "trend_long", "candle_data_unavailable")
                continue

            core_min_bars = _cfg_int("SMA20_CORE_MIN_BARS", 20)
            if len(df) < core_min_bars:
                _reject_signal(sym, "trend_long", "insufficient_history")
                continue

            sma20 = df["close"].rolling(20).mean()
            last = float(df["close"].iloc[-1])
            avg_val = sma20.iloc[-1]
            if pd.isna(avg_val):
                _reject_signal(sym, "trend_long", "sma20_unavailable")
                continue

            avg = float(avg_val)

            if last <= 0 or avg <= 0:
                _reject_signal(sym, "trend_long", "invalid_price_data")
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
                _reject_signal(sym, "trend_long", "price_not_above_required_level")
                continue

            if last > trigger:
                vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(dtype=float)
                rel_vol = 0.0
                if not vol.empty and len(vol) >= 20:
                    avg_vol = float(vol.tail(20).mean())
                    rel_vol = (float(vol.iloc[-1]) / avg_vol) if avg_vol > 0 else 0.0
                sma_prev = float(sma20.iloc[-2]) if len(sma20) >= 2 and not pd.isna(sma20.iloc[-2]) else avg
                atr = 0.0
                if all(c in df.columns for c in ("high", "low", "close")) and len(df) >= 15:
                    high = df["high"].astype(float)
                    low = df["low"].astype(float)
                    close = df["close"].astype(float)
                    prev_close = close.shift(1)
                    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
                    atr_val = tr.rolling(14).mean().iloc[-1]
                    atr = float(atr_val) if not pd.isna(atr_val) else 0.0
                signal_score = _score_momentum_setup(last, trigger, rel_vol, avg, sma_prev, atr)
                append_log(
                    "INFO",
                    "SIG",
                    f"{sym} BUY trigger last={last:.2f} sma20={avg:.2f} buffer={buffer:.4f} trigger={trigger:.2f}",
                )
                candidates.append({
                    "symbol": sym,
                    "side": "BUY",
                    "entry": float(last),
                    "sma20": avg,
                    "entry_buffer": buffer,
                    "strategy_setup": "trend_breakout",
                    "strategy_family": "trend_long",
                    "signal_score": float(signal_score),
                })
                LAST_SIGNAL_REJECT_REASONS.pop(sym, None)

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            _reject_signal(sym, "trend_long", "signal_evaluation_error")
            continue

    if not candidates:
        append_log("INFO", "SIG", "No signal found")
        return None
    candidates.sort(key=lambda x: float(x.get("signal_score") or 0.0), reverse=True)
    best = candidates[0]
    append_log("INFO", "SIG", f"momentum candidates evaluated={len(candidates)} selected={best.get('symbol')} score={float(best.get('signal_score') or 0.0):.4f}")
    return best


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def generate_mean_reversion_signal(universe):
    """Fallback signal: RSI oversold rebound or lower-BB bounce."""
    kite = get_kite()

    candidates = []
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
                _reject_signal(sym, "mean_reversion", "insufficient_history")
                continue

            close = df["close"].astype(float)
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            if pd.isna(sma20.iloc[-1]) or pd.isna(std20.iloc[-1]):
                _reject_signal(sym, "mean_reversion", "bollinger_data_unavailable")
                continue

            lower = float(sma20.iloc[-1] - (2.0 * std20.iloc[-1]))
            prev_lower = float(sma20.iloc[-2] - (2.0 * std20.iloc[-2])) if not pd.isna(std20.iloc[-2]) else lower
            rsi = _calc_rsi(close)
            rsi_last = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

            # Mean-reversion long setups: RSI oversold or lower-band bounce confirmation.
            rsi_setup = rsi_last < 30.0
            bb_bounce_setup = prev <= prev_lower and last > lower
            if not (rsi_setup or bb_bounce_setup):
                _reject_signal(sym, "mean_reversion", "mean_reversion_conditions_not_met")
                continue

            bounce_size_pct = (((last - lower) / lower) * 100.0) if lower > 0 else 0.0
            recovery_momentum_pct = (((last - prev) / prev) * 100.0) if prev > 0 else 0.0
            signal_score = _score_mean_reversion_setup(rsi_last, bounce_size_pct, recovery_momentum_pct)
            append_log(
                "INFO",
                "SIG",
                f"{sym} MR BUY trigger last={last:.2f} rsi={rsi_last:.2f} lower_bb={lower:.2f} setup={'RSI' if rsi_setup else 'BB_BOUNCE'}",
            )
            candidates.append({
                "symbol": sym,
                "side": "BUY",
                "entry": last,
                "strategy_setup": "mean_reversion",
                "strategy_family": "mean_reversion",
                "rsi": rsi_last,
                "lower_bb": lower,
                "signal_score": float(signal_score),
            })
            LAST_SIGNAL_REJECT_REASONS.pop(sym, None)

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            _reject_signal(sym, "mean_reversion", "signal_evaluation_error")
            continue

    if not candidates:
        append_log("INFO", "SIG", "No mean-reversion signal found")
        return None
    candidates.sort(key=lambda x: float(x.get("signal_score") or 0.0), reverse=True)
    best = candidates[0]
    append_log("INFO", "SIG", f"mean_reversion candidates evaluated={len(candidates)} selected={best.get('symbol')} score={float(best.get('signal_score') or 0.0):.4f}")
    return best


def generate_pullback_signal(universe):
    """Pullback continuation long: price holds above rising SMA20 and reclaims it from below."""
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
            sma20 = close.rolling(20).mean()
            if pd.isna(sma20.iloc[-1]) or pd.isna(sma20.iloc[-2]):
                continue

            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            sma_now = float(sma20.iloc[-1])
            sma_prev = float(sma20.iloc[-2])
            if sma_now <= sma_prev:
                continue

            reclaimed = prev <= sma_prev and last > sma_now
            shallow_pullback = last >= (sma_now * 0.998)
            if not (reclaimed and shallow_pullback):
                continue

            append_log(
                "INFO",
                "SIG",
                f"{sym} PULLBACK BUY trigger last={last:.2f} sma20={sma_now:.2f} prev={prev:.2f}",
            )
            return {
                "symbol": sym,
                "side": "BUY",
                "entry": last,
                "strategy_setup": "pullback_long",
                "strategy_family": "pullback_long",
                "sma20": sma_now,
            }
        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No pullback signal found")
    return None


def _score_vwap_ema_setup(dist_above_vwap_pct: float, ema_sep_pct: float, rel_vol: float, momentum_pct: float) -> float:
    dist_component = min(max(float(dist_above_vwap_pct), 0.0), 1.0) / 1.0
    ema_component = min(max(float(ema_sep_pct), 0.0), 1.0) / 1.0
    vol_component = min(max(float(rel_vol), 0.0), 3.0) / 3.0
    mom_component = min(max(float(momentum_pct), 0.0), 1.0) / 1.0
    return (
        (0.35 * dist_component)
        + (0.30 * ema_component)
        + (0.25 * vol_component)
        + (0.10 * mom_component)
    )


def generate_vwap_ema_signal(universe: list) -> dict | None:
    kite = get_kite()
    ist = ZoneInfo("Asia/Kolkata")
    fast_n = _cfg_int("VWAP_EMA_FAST", 9)
    slow_n = _cfg_int("VWAP_EMA_SLOW", 21)
    min_rel_vol = _cfg_float("VWAP_EMA_MIN_VOL_SCORE", 1.5)
    min_score = _cfg_float("VWAP_EMA_MIN_SCORE", 0.40)

    candidates = []
    for sym in universe:
        sym = (sym or "").strip().upper()
        if not sym:
            continue
        try:
            token = token_for_symbol(sym)
            data = kite.historical_data(
                token,
                pd.Timestamp.now() - pd.Timedelta(days=2),
                pd.Timestamp.now(),
                "15minute",
            )
            time.sleep(0.5)
            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                continue
            if "date" in df.columns:
                dt = pd.to_datetime(df["date"], errors="coerce")
                if getattr(dt.dt, "tz", None) is None:
                    dt = dt.dt.tz_localize(ist)
                else:
                    dt = dt.dt.tz_convert(ist)
                df = df.assign(_dt=dt)
            else:
                continue

            today = pd.Timestamp.now(tz=ist).date()
            tdf = df[df["_dt"].dt.date == today].copy()
            if len(tdf) < 10:
                continue
            if any(c not in tdf.columns for c in ("close", "volume")):
                continue

            close = tdf["close"].astype(float)
            vol = tdf["volume"].astype(float)
            if len(close) < max(21, slow_n + 1):
                continue

            vwap_num = (close * vol).cumsum()
            vwap_den = vol.cumsum().replace(0, pd.NA)
            vwap_s = vwap_num / vwap_den
            ema9_s = close.ewm(span=fast_n, adjust=False).mean()
            ema21_s = close.ewm(span=slow_n, adjust=False).mean()

            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            vwap = float(vwap_s.iloc[-1]) if not pd.isna(vwap_s.iloc[-1]) else 0.0
            ema9 = float(ema9_s.iloc[-1]) if not pd.isna(ema9_s.iloc[-1]) else 0.0
            ema21 = float(ema21_s.iloc[-1]) if not pd.isna(ema21_s.iloc[-1]) else 0.0
            avg20_vol = float(vol.tail(20).mean())
            rel_vol = (float(vol.iloc[-1]) / avg20_vol) if avg20_vol > 0 else 0.0

            # Trigger conditions (all required)
            if not (last > vwap and ema9 > ema21 and rel_vol > min_rel_vol):
                continue

            dist_pct = ((last - vwap) / vwap * 100.0) if vwap > 0 else 0.0
            ema_sep_pct = ((ema9 - ema21) / ema21 * 100.0) if ema21 > 0 else 0.0
            momentum_pct = ((last - prev) / prev * 100.0) if prev > 0 else 0.0
            score = _score_vwap_ema_setup(dist_pct, ema_sep_pct, rel_vol, momentum_pct)
            if score < min_score:
                continue

            append_log("INFO", "SIG", f"{sym} VWAP+EMA candidate score={score:.4f} last={last:.2f} vwap={vwap:.2f} ema9={ema9:.2f} ema21={ema21:.2f}")
            candidates.append(
                {
                    "symbol": sym,
                    "side": "BUY",
                    "entry": last,
                    "vwap": vwap,
                    "ema9": ema9,
                    "ema21": ema21,
                    "signal_score": float(score),
                    "strategy_tag": "vwap_ema",
                }
            )
        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    if not candidates:
        append_log("INFO", "SIG", "No VWAP+EMA signal found")
        return None
    candidates.sort(key=lambda x: float(x.get("signal_score") or 0.0), reverse=True)
    best = candidates[0]
    append_log("INFO", "SIG", f"Best VWAP+EMA signal: {best.get('symbol')} score={float(best.get('signal_score') or 0.0):.4f} (evaluated {len(candidates)} candidates)")
    return best
