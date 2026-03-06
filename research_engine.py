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


def build_longlist():
    p = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
    syms = read_symbols(p, limit=max(50, int(getattr(CFG, "UNIVERSE_SIZE", 30))))
    if not syms:
        p2 = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
        syms = read_symbols(p2, limit=50)
    return syms[:50]


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
            if df.empty or len(df) < 8:
                continue
            c0 = float(df["close"].iloc[-8])
            c1 = float(df["close"].iloc[-1])
            if c0 <= 0:
                continue
            chg = ((c1 - c0) / c0) * 100.0
            vol = float(df["volume"].tail(4).mean()) if "volume" in df.columns else 0.0
            out.append((sym, abs(chg), chg, vol))
        except Exception:
            continue
    out.sort(key=lambda x: (x[1], x[3]), reverse=True)
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
