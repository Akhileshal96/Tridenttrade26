import datetime as _dt
import pandas as pd

_LAST_VALID_SNAPSHOT = None


def _is_valid_close(close: pd.Series) -> bool:
    if close is None or close.empty:
        return False
    close = close.dropna().astype(float)
    if close.empty:
        return False
    if float(close.iloc[-1]) <= 0:
        return False
    return len(close) >= 25


def _fetch_nifty_daily_kite() -> pd.DataFrame:
    """Fetch ~90 days of daily candles for the stability symbol via Kite.

    Returns an empty DataFrame on any failure so callers can fall back gracefully.
    Columns on success: Close, High, Low (Title-cased for compatibility with
    detect_market_regime / detect_trend_direction).
    """
    try:
        from broker_zerodha import get_kite
        from instrument_store import token_for_symbol
        import config as CFG

        sym = str(getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES") or "NIFTYBEES").strip().upper()
        token = token_for_symbol(sym)
        kite = get_kite()
        to_dt = _dt.datetime.now()
        from_dt = to_dt - _dt.timedelta(days=100)  # extra buffer for weekends/holidays
        data = kite.historical_data(token, from_dt, to_dt, "day")
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        # Kite returns lowercase columns: date, open, high, low, close, volume
        rename = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "date" in df.columns:
            df = df.set_index("date")
        df = df.dropna(subset=["Close"])
        return df
    except Exception:
        return pd.DataFrame()


def detect_market_regime(df: pd.DataFrame) -> str:
    """Classify market regime from OHLCV DataFrame (expects columns Close/High/Low)."""
    if df is None or df.empty or "Close" not in df.columns:
        return "UNKNOWN"

    close = df["Close"].astype(float)
    if not _is_valid_close(close):
        return "UNKNOWN"

    nifty = float(close.iloc[-1])
    prev1 = float(close.iloc[-2]) if len(close) > 1 else nifty
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])

    chg1 = ((nifty - prev1) / prev1) * 100.0 if prev1 > 0 else 0.0

    # Granular gradient: TRENDING_UP → TRENDING → SIDEWAYS → TRENDING_DOWN → WEAK
    # Previously: "TRENDING" was returned for anything above EMA (even -0.28% days).
    # Now: TRENDING_UP requires a positive day; TRENDING_DOWN catches below-EMA selling.
    regime = "SIDEWAYS"
    if nifty > ema20 and chg1 >= 0:
        regime = "TRENDING_UP"
    elif nifty > ema20:
        # Above EMA but slightly down — could be distribution. Stay conservative.
        regime = "TRENDING"
    elif nifty < ema20 and chg1 < -0.5:
        regime = "WEAK"
    elif nifty < ema20 and chg1 <= -0.3:
        regime = "TRENDING_DOWN"

    # VOLATILE overrides all — extreme intraday range warrants reduced sizing regardless of trend.
    if all(c in df.columns for c in ["High", "Low"]):
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        tr_pct = ((high - low) / close.replace(0, pd.NA)).dropna() * 100.0
        atr_pct = float(tr_pct.tail(14).mean()) if not tr_pct.empty else 0.0
        if atr_pct >= 2.2:
            regime = "VOLATILE"

    return regime


def detect_trend_direction(df: pd.DataFrame) -> str:
    """Directional split for TRENDING routing: UP / DOWN / FLAT / UNKNOWN."""
    if df is None or df.empty or "Close" not in df.columns:
        return "UNKNOWN"
    close = df["Close"].astype(float)
    if not _is_valid_close(close):
        return "UNKNOWN"
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else last
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    chg1 = ((last - prev) / prev) * 100.0 if prev > 0 else 0.0
    if last >= ema20 and chg1 >= 0:
        return "UP"
    if last < ema20 and chg1 <= 0:
        return "DOWN"
    return "FLAT"


def get_market_regime_snapshot() -> dict:
    global _LAST_VALID_SNAPSHOT
    try:
        df = _fetch_nifty_daily_kite()
        if df is None or df.empty or "Close" not in df.columns:
            if isinstance(_LAST_VALID_SNAPSHOT, dict):
                snap = dict(_LAST_VALID_SNAPSHOT)
                snap.update({"valid_data": False, "fallback_used": True, "fallback_source": "last_valid"})
                return snap
            return {"regime": "UNKNOWN", "valid_data": False, "fallback_used": True, "fallback_source": "none"}

        close = df["Close"].astype(float)
        if not _is_valid_close(close):
            if isinstance(_LAST_VALID_SNAPSHOT, dict):
                snap = dict(_LAST_VALID_SNAPSHOT)
                snap.update({"valid_data": False, "fallback_used": True, "fallback_source": "last_valid"})
                return snap
            return {"regime": "UNKNOWN", "valid_data": False, "fallback_used": True, "fallback_source": "none"}

        nifty = float(close.iloc[-1])
        prev1 = float(close.iloc[-2]) if len(close) > 1 else nifty
        prev5 = float(close.iloc[-6]) if len(close) > 5 else prev1
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        chg1 = ((nifty - prev1) / prev1) * 100.0 if prev1 > 0 else 0.0
        chg5 = ((nifty - prev5) / prev5) * 100.0 if prev5 > 0 else 0.0

        atr_pct = 0.0
        if all(c in df.columns for c in ["High", "Low"]):
            high = df["High"].astype(float)
            low = df["Low"].astype(float)
            tr_pct = ((high - low) / close.replace(0, pd.NA)).dropna() * 100.0
            atr_pct = float(tr_pct.tail(14).mean()) if not tr_pct.empty else 0.0

        regime = detect_market_regime(df)
        trend_direction = detect_trend_direction(df)
        snap = {
            "regime": regime,
            "trend_direction": trend_direction,
            "nifty": nifty,
            "ema20": ema20,
            "chg1": chg1,
            "chg5": chg5,
            "atr_pct": atr_pct,
            "valid_data": True,
            "fallback_used": False,
            "fallback_source": "none",
        }
        _LAST_VALID_SNAPSHOT = dict(snap)
        return snap
    except Exception:
        if isinstance(_LAST_VALID_SNAPSHOT, dict):
            snap = dict(_LAST_VALID_SNAPSHOT)
            snap.update({"valid_data": False, "fallback_used": True, "fallback_source": "last_valid"})
            return snap
        return {"regime": "UNKNOWN", "valid_data": False, "fallback_used": True, "fallback_source": "none"}


def get_regime_entry_mode(regime: str) -> str:
    rg = str(regime or "UNKNOWN").upper()
    if rg == "TRENDING_UP":
        return "LONG"
    if rg == "TRENDING_DOWN":
        return "SHORT_PRIMARY"
    if rg == "WEAK":
        return "SHORT_PRIMARY"
    if rg == "TRENDING":
        return "LONG"
    if rg == "SIDEWAYS":
        return "SELECTIVE_LONG"
    if rg == "VOLATILE":
        return "RISK_REDUCED"
    return "UNKNOWN"
