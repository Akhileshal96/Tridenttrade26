import os
import time
from datetime import datetime

import pandas as pd
import yfinance as yf

import config as CFG
from log_store import append_log


DATA_DIR = os.path.join(os.getcwd(), "data")
LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

EXCLUSIONS_FILE = os.path.join(DATA_DIR, "exclusions.txt")
NIGHT_SCORES_CSV = os.path.join(DATA_DIR, "night_scores.csv")
NIGHT_REPORT_TXT = os.path.join(LOG_DIR, "night_research_report.txt")


def _load_candidates() -> list[str]:
    """
    Uses the existing universe file as candidate list if present.
    Otherwise falls back to a small default list.
    """
    if os.path.exists(CFG.UNIVERSE_PATH):
        with open(CFG.UNIVERSE_PATH, "r") as f:
            syms = [ln.strip().upper() for ln in f if ln.strip()]
        if syms:
            return syms

    # Fallback minimal list (you can expand)
    return ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LT", "SBIN", "ITC"]


def _load_exclusions() -> set[str]:
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])


def fetch_ohlc(sym: str) -> pd.DataFrame:
    """
    Fetch daily OHLC for long lookback.
    Uses auto_adjust=True and flattens MultiIndex if yfinance returns it.
    """
    df = yf.download(
        f"{sym}.NS",
        period="10y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    # Flatten if MultiIndex (seen in recent yfinance changes)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Normalize column names just in case
    cols = {c.lower(): c for c in df.columns}
    # Expect at least Close
    if "close" not in cols:
        return pd.DataFrame()

    return df


def score_symbol(df: pd.DataFrame):
    """
    Robust momentum-style score:
    - Needs at least 220 trading days
    - Score = (Close / SMA200) * 100
    Higher score => stronger trend.
    """
    if df is None or df.empty:
        return None
    if "Close" not in df.columns:
        return None
    if len(df) < 220:
        return None

    close = df["Close"]
    sma200 = close.rolling(200).mean()

    if pd.isna(sma200.iloc[-1]):
        return None

    score = (close.iloc[-1] / sma200.iloc[-1]) * 100.0
    return float(score)


def run_night_job():
    start_ts = datetime.now()
    append_log("INFO", "NIGHT", "Night research started")

    candidates = _load_candidates()
    excluded = _load_exclusions()

    # Filter excluded
    candidates = [c for c in candidates if c.upper() not in excluded]

    scored = []
    errors = 0

    for i, sym in enumerate(candidates, start=1):
        try:
            df = fetch_ohlc(sym)
            sc = score_symbol(df)
            if sc is not None:
                scored.append((sym, sc))

            # Gentle rate control for yfinance
            time.sleep(0.4)

        except Exception:
            errors += 1
            continue

    # Sort by score desc
    scored.sort(key=lambda x: x[1], reverse=True)

    # Save full ranking CSV
    df_out = pd.DataFrame(scored, columns=["symbol", "score"])
    df_out.to_csv(NIGHT_SCORES_CSV, index=False)

    # Write top universe file
    top_n = int(getattr(CFG, "UNIVERSE_SIZE", 30))
    top_syms = [s for s, _ in scored[:top_n]]

    # Ensure directory for universe file exists
    uni_dir = os.path.dirname(CFG.UNIVERSE_PATH)
    if uni_dir:
        os.makedirs(uni_dir, exist_ok=True)

    with open(CFG.UNIVERSE_PATH, "w") as f:
        for s in top_syms:
            f.write(s + "\n")

    # Create report
    end_ts = datetime.now()
    duration = (end_ts - start_ts).total_seconds()

    report_lines = []
    report_lines.append("🌙 TRIDENT NIGHT RESEARCH REPORT")
    report_lines.append(f"Timestamp: {end_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"Candidates scanned: {len(candidates)}")
    report_lines.append(f"Excluded symbols skipped: {len(excluded)}")
    report_lines.append(f"Valid scored: {len(scored)}")
    report_lines.append(f"Errors: {errors}")
    report_lines.append(f"Universe written: {len(top_syms)} -> {CFG.UNIVERSE_PATH}")
    report_lines.append(f"Scores CSV: {NIGHT_SCORES_CSV}")
    report_lines.append(f"Duration: {duration:.1f}s")
    report_lines.append("")
    report_lines.append("Top 15 symbols:")
    for s, sc in scored[:15]:
        report_lines.append(f"- {s}: {sc:.2f}")

    with open(NIGHT_REPORT_TXT, "w") as f:
        f.write("\n".join(report_lines) + "\n")

    append_log(
        "INFO",
        "NIGHT",
        f"Universe updated: {len(top_syms)} symbols | scored={len(scored)} | errors={errors}",
    )
