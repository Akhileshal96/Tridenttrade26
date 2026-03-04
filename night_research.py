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

EXCLUSIONS_FILE = getattr(CFG, "EXCLUSIONS_PATH", os.path.join(DATA_DIR, "exclusions.txt"))
NIGHT_SCORES_CSV = os.path.join(DATA_DIR, "night_scores.csv")
NIGHT_REPORT_TXT = os.path.join(LOG_DIR, "night_research_report.txt")

def _load_candidates():
    cand_path = os.path.join(DATA_DIR, "candidates.txt")
    if os.path.exists(cand_path):
        with open(cand_path, "r") as f:
            syms = [ln.strip().upper() for ln in f if ln.strip()]
        if syms:
            return syms

    # fallback to existing universe
    for p in [getattr(CFG, "UNIVERSE_LIVE_PATH", ""), getattr(CFG, "UNIVERSE_TRADING_PATH", ""), getattr(CFG, "UNIVERSE_PATH", "")]:
        if p and os.path.exists(p):
            with open(p, "r") as f:
                syms = [ln.strip().upper() for ln in f if ln.strip()]
            if syms:
                return syms

    return ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LT", "SBIN", "ITC"]

def _load_exclusions():
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])

def fetch_ohlc(sym):
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
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return pd.DataFrame()
    return df

def score_symbol(df):
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if len(df) < 220:
        return None
    close = df["Close"]
    sma200 = close.rolling(200).mean()
    if pd.isna(sma200.iloc[-1]) or float(sma200.iloc[-1]) <= 0:
        return None
    return float((close.iloc[-1] / sma200.iloc[-1]) * 100.0)

def run_night_job():
    start_ts = datetime.now()
    append_log("INFO", "NIGHT", "Night research started")

    candidates = _load_candidates()
    excluded = _load_exclusions()
    candidates = [c for c in candidates if c and c.upper() not in excluded]

    scored = []
    errors = 0

    for sym in candidates:
        try:
            df = fetch_ohlc(sym)
            sc = score_symbol(df)
            if sc is not None:
                scored.append((sym, sc))
            time.sleep(0.4)
        except Exception:
            errors += 1
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    pd.DataFrame(scored, columns=["symbol", "score"]).to_csv(NIGHT_SCORES_CSV, index=False)

    top_n = int(getattr(CFG, "UNIVERSE_SIZE", 30))
    top_syms = [s for s, _ in scored[:top_n]]

    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    os.makedirs(os.path.dirname(live_path), exist_ok=True)

    tmp_path = live_path + ".tmp"
    with open(tmp_path, "w") as f:
        for s in top_syms:
            f.write(s + "\n")
    os.replace(tmp_path, live_path)

    end_ts = datetime.now()
    duration = (end_ts - start_ts).total_seconds()
    report = []
    report.append("🌙 TRIDENT NIGHT RESEARCH REPORT")
    report.append(f"Timestamp: {end_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Candidates scanned: {len(candidates)}")
    report.append(f"Excluded symbols: {len(excluded)}")
    report.append(f"Valid scored: {len(scored)}")
    report.append(f"Errors: {errors}")
    report.append(f"Universe live written: {len(top_syms)} -> {live_path}")
    report.append(f"Scores CSV: {NIGHT_SCORES_CSV}")
    report.append(f"Duration: {duration:.1f}s")
    report.append("")
    report.append("Top 15 symbols:")
    for s, sc in scored[:15]:
        report.append(f"- {s}: {sc:.2f}")

    with open(NIGHT_REPORT_TXT, "w") as f:
        f.write("\n".join(report) + "\n")

    append_log("INFO", "NIGHT", f"Universe live updated: {len(top_syms)} | scored={len(scored)} | errors={errors}")
