import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config as CFG
import night_research
import research_engine
from log_store import append_log
from universe_builder import save_universe

IST = ZoneInfo("Asia/Kolkata")


def run_nightly_maintenance(state=None):
    """Run nightly research once per day, then build/store research universe."""
    now = datetime.now(IST)
    run_key = now.strftime("%Y-%m-%d")
    if isinstance(state, dict) and state.get("last_night_research_day") == run_key:
        append_log("INFO", "UNIV", "Night research already completed today; skipping rebuild")
        return

    append_log("INFO", "UNIV", "=== NIGHT RESEARCH START ===")
    night_research.run_night_job()
    rstate = research_engine.run_night_research()
    universe = list(rstate.get("research_universe") or [])

    if universe:
        if isinstance(state, dict):
            state["research_universe"] = list(universe)
            state["last_night_research_day"] = run_key
        out = save_universe(universe, getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt")))
        append_log("INFO", "UNIV", f"dynamic universe built size={len(universe)} path={out}")
    else:
        append_log("WARN", "UNIV", "dynamic universe empty; keeping static fallback")

    append_log("INFO", "UNIV", "=== NIGHT RESEARCH END ===")
