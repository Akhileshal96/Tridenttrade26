import os
from datetime import datetime

import config as CFG
from log_store import append_log
import universe_builder as UB
from universe_builder import build_dynamic_universe, save_universe

DATA_DIR = os.path.join(os.getcwd(), "data")
LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

NIGHT_REPORT_TXT = os.path.join(LOG_DIR, "night_research_report.txt")
NIGHT_LOG_TXT = os.path.join(LOG_DIR, "night_research.log")


def _night_log(line: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(NIGHT_LOG_TXT, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {line}\n")


def last_report_summary():
    if not os.path.exists(NIGHT_REPORT_TXT):
        return "(night report not generated yet. Run /nightnow)"
    with open(NIGHT_REPORT_TXT, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read().strip()
    return txt or "(night report is empty)"


def read_night_log_tail(n=120):
    if not os.path.exists(NIGHT_LOG_TXT):
        return "(no night research logs yet. Run /nightnow)"
    with open(NIGHT_LOG_TXT, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    out = "".join(lines[-max(1, int(n)):]).strip()
    return out or "(no night research logs yet)"


def run_night_job():
    append_log("INFO", "NIGHT", "Night research started")
    details_fn = getattr(UB, "build_dynamic_universe_details", None)
    if callable(details_fn):
        details = details_fn(target_size=int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20))) or {}
    else:
        selected = list(build_dynamic_universe(target_size=int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20))) or [])
        details = {"selected": selected}

    candidates_loaded = int(details.get("candidates_loaded") or 0)
    excluded = int(details.get("excluded") or 0)
    to_scan = int(details.get("to_scan") or 0)
    scored = int(details.get("scored") or 0)
    errors = int(details.get("errors") or 0)
    selected = list(details.get("selected") or [])
    sector_leaders = list(details.get("sector_leaders") or [])
    top_ranked = list(details.get("top_ranked") or [])

    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    write_path = save_universe(selected, live_path) if selected else live_path

    _night_log("=== NIGHT RESEARCH START ===")
    _night_log(f"Candidates loaded: {candidates_loaded}")
    _night_log(f"Excluded symbols: {excluded}")
    _night_log(f"To scan: {to_scan}")
    _night_log(f"Stocks scored: {scored}")
    _night_log(f"Errors: {errors}")
    _night_log("Top sectors:")
    for sec, score in sector_leaders[:5]:
        _night_log(f"{sec} score={float(score):.2f}")
    _night_log("Top ranked stocks:")
    for r in top_ranked[:10]:
        _night_log(f"{r.get('symbol')} score={float(r.get('final_score') or 0.0):.3f}")
    _night_log(f"Selected universe size: {len(selected)}")
    _night_log("=== NIGHT RESEARCH END ===")

    report = [
        "🌙 TRIDENT NIGHT RESEARCH REPORT",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Candidates scanned: {to_scan}",
        f"Excluded symbols: {excluded}",
        f"Valid scored: {scored}",
        f"Errors: {errors}",
        f"Universe live written: {len(selected)}",
        f"Path written: {write_path}",
        "Top sectors:",
    ]
    for sec, _score in sector_leaders[:5]:
        report.append(f"- {sec}")
    report.append("Top ranked symbols:")
    for r in top_ranked[:10]:
        report.append(f"- {r.get('symbol')}: {float(r.get('final_score') or 0.0):.3f}")

    with open(NIGHT_REPORT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    append_log("INFO", "NIGHT", f"Universe live updated: {len(selected)} | scored={scored} | errors={errors}")
    return {
        "selected": list(selected),
        "details": dict(details),
        "write_path": write_path,
    }
