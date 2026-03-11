import os
from typing import List

import pandas as pd
import yfinance as yf

import config as CFG
from log_store import append_log

# NIFTY 100 seed universe (NSE symbols, without .NS suffix)
NIFTY100_SYMBOLS = [
    "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ATGL", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT",
    "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BANDHANBNK", "BANKBARODA", "BEL",
    "BERGEPAINT", "BHARTIARTL", "BHEL", "BIOCON", "BPCL", "BRITANNIA", "CANBK", "CHOLAFIN", "CIPLA",
    "COALINDIA", "DABUR", "DIVISLAB", "DLF", "DRREDDY", "EICHERMOT", "GAIL", "GODREJCP", "GRASIM",
    "HAVELLS", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB", "INDIGO", "INDUSINDBK", "INFY", "IOC", "IRCTC", "ITC",
    "JINDALSTEL", "JSWSTEEL", "KOTAKBANK", "LT", "LTIM", "M&M", "MARICO", "MARUTI", "MCDOWELL-N",
    "MOTHERSUMI", "MPHASIS", "NESTLEIND", "NHPC", "NMDC", "NTPC", "ONGC", "PAGEIND", "PEL", "PFC",
    "PIDILITIND", "PNB", "POWERGRID", "RECLTD", "RELIANCE", "SAIL", "SBICARD", "SBILIFE", "SBIN",
    "SHREECEM", "SIEMENS", "SRF", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATAPOWER", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UPL", "VEDL", "WIPRO", "ZYDUSLIFE",
]


def _fetch_history(symbols: List[str], period="2mo", interval="1d") -> pd.DataFrame:
    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(
        tickers=tickers,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
        group_by="ticker",
    )
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def build_dynamic_universe(min_size: int = 10, max_size: int = 20) -> List[str]:
    symbols = list(NIFTY100_SYMBOLS)
    hist = _fetch_history(symbols)
    if hist.empty:
        append_log("WARN", "UNIV", "dynamic universe fetch failed/empty")
        return []

    rows = []
    for sym in symbols:
        tk = f"{sym}.NS"
        try:
            if tk in hist.columns.get_level_values(0):
                sdf = hist[tk]
            else:
                continue
            if sdf is None or sdf.empty:
                continue
            close = sdf.get("Close")
            vol = sdf.get("Volume")
            if close is None or vol is None or len(close) < 25 or len(vol) < 25:
                continue

            close = close.astype(float)
            vol = vol.astype(float)
            price = float(close.iloc[-1])
            prev_close = float(close.iloc[-2]) if len(close) > 1 else price
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            avg_vol_20 = float(vol.tail(20).mean())
            vol_today = float(vol.iloc[-1])

            # Step 1: liquidity
            if avg_vol_20 <= 1_000_000 or price <= 100:
                continue

            # Step 2: momentum
            if not (price > ema20 and vol_today > 1.5 * avg_vol_20):
                continue

            pct_change = ((price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
            rows.append((sym, pct_change, price, avg_vol_20, vol_today))
        except Exception:
            continue

    if not rows:
        append_log("WARN", "UNIV", "dynamic universe filters produced empty list")
        return []

    rows.sort(key=lambda x: x[1], reverse=True)
    top = [r[0] for r in rows[: max(1, int(max_size))]]

    if len(top) < int(min_size):
        append_log("WARN", "UNIV", f"dynamic universe below min size={len(top)}<{min_size}")
        return []

    append_log("INFO", "UNIV", "top movers selected")
    append_log("INFO", "UNIV", f"dynamic universe built size={len(top)}")
    return top


def save_universe(symbols: List[str], path: str = None) -> str:
    path = path or getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in symbols:
            if s:
                f.write(str(s).strip().upper() + "\n")
    os.replace(tmp, path)
    return path


def is_market_regime_ok() -> bool:
    try:
        df = yf.download("^NSEI", period="2mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty or "Close" not in df.columns or len(df) < 25:
            append_log("WARN", "MARKET", "NIFTY data unavailable; allowing trade")
            return True

        close = df["Close"].astype(float)
        nifty = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else nifty
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        pct = ((nifty - prev) / prev) * 100.0 if prev > 0 else 0.0

        ok = (nifty > ema20) or (pct > -0.5)
        if ok:
            append_log("INFO", "MARKET", "NIFTY trend bullish")
        else:
            append_log("WARN", "MARKET", "Weak market regime → skipping trade")
        return ok
    except Exception as e:
        append_log("WARN", "MARKET", f"Regime check failed ({e}); allowing trade")
        return True
