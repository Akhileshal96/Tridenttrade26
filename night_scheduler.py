import os

import config as CFG
import night_research
from log_store import append_log
from universe_builder import build_dynamic_universe, save_universe


def run_nightly_maintenance(state=None):
    """Run existing night research + dynamic universe build/save."""
    night_research.run_night_job()

    dynamic_universe = build_dynamic_universe(min_size=10, max_size=20)
    if dynamic_universe:
        if isinstance(state, dict):
            state["dynamic_universe"] = list(dynamic_universe)
        out = save_universe(dynamic_universe, getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt")))
        append_log("INFO", "UNIV", f"dynamic universe built size={len(dynamic_universe)} path={out}")
    else:
        append_log("WARN", "UNIV", "dynamic universe empty; keeping static fallback")
