import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config as CFG
import night_research
import research_engine
from log_store import append_log
from universe_builder import save_universe

IST = ZoneInfo("Asia/Kolkata")
RUN_MARKER = os.path.join(os.getcwd(), "data", "night_research_day.txt")


def _read_marker_day() -> str:
    try:
        if not os.path.exists(RUN_MARKER):
            return ""
        with open(RUN_MARKER, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_marker_day(day: str) -> None:
    try:
        os.makedirs(os.path.dirname(RUN_MARKER), exist_ok=True)
        with open(RUN_MARKER, "w", encoding="utf-8") as f:
            f.write(str(day).strip())
    except Exception as e:
        append_log("WARN", "NIGHT", f"marker write failed: {e}")


def run_nightly_maintenance(state=None, force: bool = False):
    """Run nightly research once per day, then build/store research universe."""
    now = datetime.now(IST)
    run_key = now.strftime("%Y-%m-%d")

    marker_day = _read_marker_day()
    if (not force) and marker_day == run_key:
        append_log("INFO", "UNIV", "Night research already completed today (marker); skipping rebuild")
        if isinstance(state, dict):
            state["last_night_research_day"] = run_key
        return

    if (not force) and isinstance(state, dict) and state.get("last_night_research_day") == run_key:
        append_log("INFO", "UNIV", "Night research already completed today; skipping rebuild")
        _write_marker_day(run_key)
        return

    append_log("INFO", "UNIV", "=== NIGHT RESEARCH START ===")
    result = night_research.run_night_job() or {}
    details = result.get("details") if isinstance(result, dict) else {}
    universe = list((result.get("selected") if isinstance(result, dict) else None) or [])

    if universe:
        research_engine.apply_night_universe(universe, details=details if isinstance(details, dict) else None)
        if isinstance(state, dict):
            state["research_universe"] = list(universe)
            state["last_night_research_day"] = run_key
        out = save_universe(universe, getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt")))
        append_log("INFO", "UNIV", f"dynamic universe built size={len(universe)} path={out}")
        _write_marker_day(run_key)
    else:
        append_log("WARN", "UNIV", "dynamic universe empty; keeping static fallback")

    append_log("INFO", "UNIV", "=== NIGHT RESEARCH END ===")
