from datetime import datetime
from zoneinfo import ZoneInfo
import os
import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log
from market_regime import detect_market_regime
from universe_manager import read_symbols, should_refresh

IST = ZoneInfo("Asia/Kolkata")

research_state = {
    "longlist": [],
    "top_movers": [],
    "trading_universe": [],
    "market_regime": "WEAK",
    "last_refresh": None,
}


def _passes_filters(df: pd.DataFrame) -> bool:
    if df is None or df.empty or len(df) < 30:
        return False
    if not all(c in df.columns for c in ["close", "high", "low", "volume"]):
        return False

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)

    if close.iloc[-1] <= 0:
        return False

    # Liquidity / volume
    if float(vol.tail(20).mean()) < float(os.getenv("LONG_MIN_AVG_VOL", "50000")):
        return False

    # Proxy spread: average intrabar range
    spread_proxy = (((high - low) / close.replace(0, pd.NA)).dropna() * 100.0).tail(20).mean()
    if float(spread_proxy or 0.0) > float(os.getenv("LONG_MAX_SPREAD_PCT", "1.8")):
        return False

    # Volatility bounds
    ret = close.pct_change().dropna()
    vol_pct = float(ret.tail(20).std() * 100.0) if not ret.empty else 0.0
    min_v = float(os.getenv("LONG_MIN_VOL_PCT", "0.15"))
    max_v = float(os.getenv("LONG_MAX_VOL_PCT", "3.0"))
    if not (min_v <= vol_pct <= max_v):
        return False

    # Trend quality
    sma20 = close.rolling(20).mean().iloc[-1]
    if pd.isna(sma20) or float(close.iloc[-1]) < float(sma20):
        return False

    return True


def build_longlist():
    p = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
    base_syms = read_symbols(p, limit=120)
    if not base_syms:
        p2 = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
        base_syms = read_symbols(p2, limit=120)

    out = []
    kite = None
    try:
        kite = get_kite()
    except Exception:
        pass

    for sym in base_syms:
        if len(out) >= 50:
            break
        if not kite:
            out.append(sym)
            continue
        try:
            token = token_for_symbol(sym)
            data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=8), pd.Timestamp.now(), "15minute")
            df = pd.DataFrame(data)
            if _passes_filters(df):
                out.append(sym)
        except Exception:
            continue

    if len(out) < 30:
        # graceful fallback to keep universe populated
        for s in base_syms:
            if s not in out:
                out.append(s)
            if len(out) >= 30:
                break

    return out[:50]


def _score_top_mover(df: pd.DataFrame) -> float:
    close = df["close"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series([0.0] * len(df))

    c0 = float(close.iloc[-8]) if len(close) >= 8 else float(close.iloc[0])
    c1 = float(close.iloc[-1])
    chg_pct = ((c1 - c0) / c0 * 100.0) if c0 > 0 else 0.0

    rv = float(vol.tail(4).mean() / max(vol.tail(20).mean(), 1.0))
    momentum = float(((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100.0) if len(close) >= 2 and close.iloc[-2] > 0 else 0.0)
    vwap = float((close * vol).sum() / max(float(vol.sum()), 1.0))
    vwap_dist = float(((c1 - vwap) / vwap) * 100.0) if vwap > 0 else 0.0

    return abs(chg_pct) * 0.45 + rv * 0.20 + abs(momentum) * 0.20 + abs(vwap_dist) * 0.15


def build_live_top_movers(longlist):
    out = []
    kite = None
    try:
        kite = get_kite()
    except Exception:
        return longlist[: int(getattr(CFG, "UNIVERSE_SIZE", 30))]

    for sym in longlist[:60]:
        try:
            token = token_for_symbol(sym)
            data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=2), pd.Timestamp.now(), "15minute")
            df = pd.DataFrame(data)
            if df.empty or len(df) < 12 or "close" not in df.columns:
                continue
            score = _score_top_mover(df)
            out.append((sym, score))
        except Exception:
            continue

    out.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in out[: int(getattr(CFG, "UNIVERSE_SIZE", 30))]]


def detect_market_regime_state(longlist):
    if not longlist:
        return "WEAK"
    try:
        kite = get_kite()
        token = token_for_symbol(longlist[0])
        data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=3), pd.Timestamp.now(), "15minute")
        return detect_market_regime(pd.DataFrame(data))
    except Exception:
        return "WEAK"


def run_night_research():
    longlist = build_longlist()
    regime = detect_market_regime_state(longlist)
    research_state.update(
        {
            "longlist": longlist,
            "top_movers": longlist[: int(getattr(CFG, "UNIVERSE_SIZE", 30))],
            "trading_universe": longlist[: int(getattr(CFG, "UNIVERSE_SIZE", 30))],
            "market_regime": regime,
            "last_refresh": datetime.now(IST),
        }
    )
    append_log("INFO", "RESEARCH", f"Longlist built: {len(longlist)} symbols")
    append_log("INFO", "REGIME", f"Market regime = {regime}")
    return research_state


def get_trading_universe(force=False):
    if force or should_refresh(research_state.get("last_refresh"), interval_min=10):
        longlist = build_longlist()
        top = build_live_top_movers(longlist)
        regime = detect_market_regime_state(longlist)
        research_state.update(
            {
                "longlist": longlist,
                "top_movers": top,
                "trading_universe": top,
                "market_regime": regime,
                "last_refresh": datetime.now(IST),
            }
        )
        append_log("INFO", "RESEARCH", f"Top movers refreshed: {len(top)}")
        append_log("INFO", "REGIME", f"Market regime = {regime}")
    return research_state
