import pandas as pd
import yfinance as yf


def detect_market_regime(df: pd.DataFrame) -> str:
    """Classify market regime from OHLCV DataFrame (expects columns Close/High/Low)."""
    if df is None or df.empty or "Close" not in df.columns:
        return "WEAK"

    close = df["Close"].astype(float)
    if len(close) < 25:
        return "WEAK"

    nifty = float(close.iloc[-1])
    prev1 = float(close.iloc[-2]) if len(close) > 1 else nifty
    prev5 = float(close.iloc[-6]) if len(close) > 5 else prev1
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])

    chg1 = ((nifty - prev1) / prev1) * 100.0 if prev1 > 0 else 0.0
    chg5 = ((nifty - prev5) / prev5) * 100.0 if prev5 > 0 else 0.0

    regime = "SIDEWAYS"
    if nifty > ema20 and chg1 > -0.3:
        regime = "TRENDING"
    elif nifty < ema20 and chg1 < -0.5:
        regime = "WEAK"

    # Optional volatility gate via ATR proxy
    if all(c in df.columns for c in ["High", "Low"]):
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        tr_pct = ((high - low) / close.replace(0, pd.NA)).dropna() * 100.0
        atr_pct = float(tr_pct.tail(14).mean()) if not tr_pct.empty else 0.0
        if atr_pct >= 2.2:
            regime = "VOLATILE"

    return regime


def get_market_regime_snapshot() -> dict:
    try:
        df = yf.download("^NSEI", period="3mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty or "Close" not in df.columns:
            return {"regime": "WEAK", "nifty": 0.0, "ema20": 0.0, "chg1": 0.0, "chg5": 0.0, "atr_pct": 0.0}

        close = df["Close"].astype(float)
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
        return {
            "regime": regime,
            "nifty": nifty,
            "ema20": ema20,
            "chg1": chg1,
            "chg5": chg5,
            "atr_pct": atr_pct,
        }
    except Exception:
        return {"regime": "WEAK", "nifty": 0.0, "ema20": 0.0, "chg1": 0.0, "chg5": 0.0, "atr_pct": 0.0}
