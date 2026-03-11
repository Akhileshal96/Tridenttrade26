import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log
from universe_builder import build_dynamic_universe_details, save_universe
from universe_manager import read_symbols, should_refresh

IST = ZoneInfo("Asia/Kolkata")

research_state = {
    "research_universe": [],
    "trading_universe": [],
    "last_night_research": None,
    "last_refresh": None,
    "last_heavy_refresh": None,
    "last_report": {},
}


def _is_heavy_refresh_due() -> bool:
    last = research_state.get("last_heavy_refresh")
    return should_refresh(last, interval_min=int(getattr(CFG, "INTRADAY_HEAVY_REFRESH_MIN", 30)))


def _static_fallback_universe(limit=30):
    p = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
    syms = read_symbols(p, limit=limit)
    if syms:
        return syms
    p2 = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
    syms = read_symbols(p2, limit=limit)
    if syms:
        return syms
    p3 = getattr(CFG, "UNIVERSE_PATH", os.path.join(os.getcwd(), "data", "universe.txt"))
    return read_symbols(p3, limit=limit)


def run_night_research():
    append_log("INFO", "UNIV", "=== NIGHT RESEARCH START ===")
    details = build_dynamic_universe_details(target_size=int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20)))
    uni = list(details.get("selected") or [])

    if not uni:
        prev = list(research_state.get("research_universe") or [])
        if prev:
            uni = prev
            append_log("WARN", "UNIV", "dynamic build failed; fallback to previous research universe")
        else:
            uni = _static_fallback_universe(limit=int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20)))
            append_log("WARN", "UNIV", "dynamic build failed; fallback to static universe")

    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
    save_universe(uni, live_path)

    research_state["research_universe"] = list(uni)
    research_state["trading_universe"] = list(uni[: int(getattr(CFG, "UNIVERSE_SIZE", 20))])
    research_state["last_night_research"] = datetime.now(IST)
    research_state["last_refresh"] = datetime.now(IST)
    research_state["last_heavy_refresh"] = research_state["last_refresh"]
    research_state["last_report"] = details

    append_log("INFO", "UNIV", f"Selected universe size: {len(uni)}")
    append_log("INFO", "UNIV", "=== NIGHT RESEARCH END ===")
    return research_state


def refresh_top_movers_from_research():
    target = int(getattr(CFG, "UNIVERSE_SIZE", 20))
    base = list(research_state.get("research_universe") or [])

    if bool(getattr(CFG, "INTRADAY_DYNAMIC_REFRESH", True)) and _is_heavy_refresh_due():
        details = build_dynamic_universe_details(target_size=target)
        fresh = list(details.get("selected") or [])
        if fresh:
            curr = list(research_state.get("trading_universe") or base[:target])
            max_swaps = max(0, int(getattr(CFG, "INTRADAY_REFRESH_MAX_SWAPS", 3) or 3))

            incoming = [s for s in fresh if s not in curr]
            outgoing = [s for s in curr if s not in fresh]
            swap_n = min(max_swaps, len(incoming), len(outgoing))

            drop_set = set(outgoing[:swap_n])
            blended = [s for s in curr if s not in drop_set]
            blended.extend(incoming[:swap_n])

            for s in fresh + curr:
                if s not in blended:
                    blended.append(s)
                if len(blended) >= target:
                    break

            top = blended[:target]
            now = datetime.now(IST)
            research_state["research_universe"] = list(fresh)
            research_state["trading_universe"] = top
            research_state["last_refresh"] = now
            research_state["last_heavy_refresh"] = now
            research_state["last_report"] = details
            append_log("INFO", "UNIV", f"Adaptive intraday refresh size={len(top)} swaps={swap_n}")
            return top

    if not base:
        return []

    top = base[:target]
    research_state["trading_universe"] = top
    research_state["last_refresh"] = datetime.now(IST)
    return top


def get_trading_universe(force=False):
    now = datetime.now(IST)
    if force or not research_state.get("research_universe"):
        run_night_research()
        return research_state

    in_market = now.replace(hour=9, minute=15, second=0, microsecond=0) <= now <= now.replace(hour=15, minute=30, second=0, microsecond=0)
    if in_market and should_refresh(research_state.get("last_refresh"), interval_min=int(getattr(CFG, "MARKET_REFRESH_MIN", 10))):
        refresh_top_movers_from_research()

    if not research_state.get("trading_universe"):
        research_state["trading_universe"] = list(research_state.get("research_universe") or [])

    return research_state
