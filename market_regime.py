import pandas as pd


def detect_market_regime(df: pd.DataFrame) -> str:
    if df is None or df.empty or "close" not in df.columns:
        return "WEAK"
    close = df["close"].astype(float)
    if len(close) < 20:
        return "WEAK"
    ret = close.pct_change().dropna()
    vol = float(ret.std() * 100.0)
    drift = float((close.iloc[-1] / close.iloc[max(0, len(close)-20)] - 1.0) * 100.0)
    if vol > 2.0:
        return "VOLATILE"
    if abs(drift) < 0.6:
        return "SIDEWAYS"
    if drift > 0:
        return "TRENDING"
    return "WEAK"
