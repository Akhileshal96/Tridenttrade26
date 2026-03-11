import os
import time
from collections import defaultdict
from typing import Dict, List

import pandas as pd
import yfinance as yf

import config as CFG
from excluded_store import load_excluded
from instrument_store import INSTR_PATH, refresh_instruments
from log_store import append_log
from market_regime import get_market_regime_snapshot

SECTOR_MAP: Dict[str, str] = {
    "SBIN": "BANKING", "ICICIBANK": "BANKING", "HDFCBANK": "BANKING", "KOTAKBANK": "BANKING", "AXISBANK": "BANKING",
    "INFY": "IT", "TCS": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY", "IOC": "ENERGY",
    "ITC": "FMCG", "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG", "DABUR": "FMCG",
    "LT": "CAPITAL_GOODS", "SIEMENS": "CAPITAL_GOODS", "BHEL": "CAPITAL_GOODS",
    "TATAMOTORS": "AUTO", "MARUTI": "AUTO", "M&M": "AUTO", "TVSMOTOR": "AUTO",
    "HAL": "DEFENCE", "BEL": "DEFENCE", "BEML": "DEFENCE",
}

NIFTY100_SYMBOLS = [
    "ABB", "ABCAPITAL", "ABFRL", "ACC", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP",
    "APOLLOTYRE", "ASHOKLEY", "ASIANPAINT", "ASTRAL", "ATGL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV",
    "BAJFINANCE", "BALKRISIND", "BANKBARODA", "BEL", "BERGEPAINT", "BHARATFORG", "BHARTIARTL", "BHEL", "BIOCON", "BPCL",
    "BRITANNIA", "BSOFT", "CANBK", "CHOLAFIN", "CIPLA", "COALINDIA", "CONCOR", "DABUR", "DIVISLAB", "DLF",
    "DRREDDY", "EICHERMOT", "GAIL", "GODREJCP", "GRASIM", "HAL", "HAVELLS", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB", "INDHOTEL",
    "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY", "IOC", "IRCTC", "ITC", "JINDALSTEL", "JSWENERGY", "JSWSTEEL",
    "JUBLFOOD", "KOTAKBANK", "LTF", "LT", "LTIM", "M&M", "MARICO", "MARUTI", "MOTHERSON", "MPHASIS",
    "NAUKRI", "NESTLEIND", "NMDC", "NTPC", "ONGC", "PAGEIND", "PEL", "PFC", "PIDILITIND", "PNB",
    "POLYCAB", "POWERGRID", "RECLTD", "RELIANCE", "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHRIRAMFIN", "SIEMENS",
]

_DOWNLOAD_CACHE = {}


def _cache_get(key):
    ttl = int(getattr(CFG, "UNIVERSE_CACHE_TTL_SEC", 600) or 600)
    rec = _DOWNLOAD_CACHE.get(key)
    if not rec:
        return None
    ts, data = rec
    if (time.time() - ts) > max(1, ttl):
        _DOWNLOAD_CACHE.pop(key, None)
        return None
    return data


def _cache_put(key, data):
    _DOWNLOAD_CACHE[key] = (time.time(), data)


def _download_cached(symbols: List[str], period="6mo", interval="1d") -> pd.DataFrame:
    key = (tuple(symbols), str(period), str(interval))
    cached = _cache_get(key)
    if isinstance(cached, pd.DataFrame):
        return cached
    df = _download(symbols, period=period, interval=interval)
    if isinstance(df, pd.DataFrame) and not df.empty:
        _cache_put(key, df)
    return df


def _download_nifty_cached(period="3mo", interval="1d") -> pd.DataFrame:
    key = ("^NSEI", str(period), str(interval))
    cached = _cache_get(key)
    if isinstance(cached, pd.DataFrame):
        return cached
    df = yf.download("^NSEI", period=period, interval=interval, auto_adjust=False, progress=False, threads=False)
    if isinstance(df, pd.DataFrame) and not df.empty:
        _cache_put(key, df)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _normalize_symbols(values: List[str]) -> List[str]:
    return list(dict.fromkeys([str(s).strip().upper() for s in values if str(s).strip()]))


def _symbols_from_candidates_file(path: str) -> List[str]:
    try:
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return _normalize_symbols([line for line in f.read().splitlines() if line.strip()])
    except Exception:
        return []


def _persist_candidates(path: str, symbols: List[str]) -> None:
    try:
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for s in _normalize_symbols(symbols):
                f.write(s + "\n")
    except Exception as e:
        append_log("WARN", "UNIV", f"could not persist candidates: {e}")


def _load_nifty100_from_repo() -> List[str]:
    path = os.path.join(os.getcwd(), "data", "nifty100.txt")
    syms = _symbols_from_candidates_file(path)
    if len(syms) >= 90:
        return syms
    return []


def load_nifty100_symbols() -> List[str]:
    # Always prefer NIFTY100 broad pool. Env/file override allowed only if broad enough.
    repo_syms = _load_nifty100_from_repo()
    base = repo_syms if repo_syms else _normalize_symbols(NIFTY100_SYMBOLS)

    env_csv = str(getattr(CFG, "CANDIDATE_SYMBOLS", "") or "").strip()
    if env_csv:
        env_syms = _normalize_symbols(env_csv.split(","))
        if len(env_syms) >= 50:
            append_log("INFO", "UNIV", f"Candidates loaded from env: {len(env_syms)}")
            return env_syms
        append_log("WARN", "UNIV", f"Ignoring narrow env candidates ({len(env_syms)}); using NIFTY100")

    file_path = str(getattr(CFG, "CANDIDATES_PATH", "") or "").strip()
    file_syms = _symbols_from_candidates_file(file_path)
    if len(file_syms) >= 50:
        append_log("INFO", "UNIV", f"Candidates loaded from file: {len(file_syms)} ({file_path})")
        return file_syms
    if file_syms:
        append_log("WARN", "UNIV", f"Ignoring narrow file candidates ({len(file_syms)}); using NIFTY100")

    append_log("INFO", "UNIV", f"Candidates loaded: {len(base)}")
    return base


def _download(symbols: List[str], period="6mo", interval="1d") -> pd.DataFrame:
    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(tickers=tickers, period=period, interval=interval, auto_adjust=False, progress=False, threads=False, group_by="ticker")
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _stock_metrics(sym: str, sdf: pd.DataFrame, nifty_20d_return: float) -> dict | None:
    try:
        close = sdf["Close"].astype(float)
        high = sdf["High"].astype(float)
        low = sdf["Low"].astype(float)
        vol = sdf["Volume"].astype(float)
        if len(close) < 220 or len(vol) < 25:
            return None

        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) > 1 else price
        close_20d = float(close.iloc[-21]) if len(close) > 20 else prev_close
        sma200 = float(close.rolling(200).mean().iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])

        avg_vol_20 = float(vol.tail(20).mean())
        vol_today = float(vol.iloc[-1])

        if avg_vol_20 <= 1_000_000 or price <= 100:
            return None
        if not (price > ema20 and vol_today > 1.5 * avg_vol_20):
            return None

        trend_score = (price / sma200) if sma200 > 0 else 0.0
        stock_return_20d = ((price - close_20d) / close_20d) if close_20d > 0 else 0.0
        rs_score = stock_return_20d - nifty_20d_return
        volume_score = (vol_today / avg_vol_20) if avg_vol_20 > 0 else 0.0

        tr = (high - low).abs()
        atr14 = float(tr.tail(14).mean()) if len(tr) >= 14 else float(tr.mean())
        atr_score = (atr14 / price) if price > 0 else 0.0

        pct_change = ((price - prev_close) / prev_close) if prev_close > 0 else 0.0

        sector = SECTOR_MAP.get(sym, "OTHER")
        return {
            "symbol": sym,
            "sector": sector,
            "price": price,
            "pct_change": pct_change,
            "trend_score": trend_score,
            "rs_score": rs_score,
            "volume_score": volume_score,
            "atr_score": atr_score,
        }
    except Exception:
        return None


def _sector_rotation_scores(rows: List[dict]) -> Dict[str, float]:
    bucket = defaultdict(list)
    for r in rows:
        bucket[r["sector"]].append(r)

    sector_scores = {}
    for sec, arr in bucket.items():
        avg_rs = float(sum(x["rs_score"] for x in arr) / max(1, len(arr)))
        avg_vol = float(sum(x["volume_score"] for x in arr) / max(1, len(arr)))
        avg_5d = float(sum(x["pct_change"] for x in arr) / max(1, len(arr)))
        sector_scores[sec] = (0.45 * avg_5d) + (0.35 * avg_rs) + (0.20 * avg_vol)

    ranked = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
    if ranked:
        append_log("INFO", "SECTOR", "Top sectors:")
        for sec, score in ranked[:5]:
            append_log("INFO", "SECTOR", f"{sec} score={score:.2f}")
    return sector_scores


def build_dynamic_universe_details(target_size: int = None) -> dict:
    target_size = int(target_size or getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20))
    candidates_all = load_nifty100_symbols()
    excluded = set(load_excluded())
    candidates = [s for s in candidates_all if s not in excluded]

    append_log("INFO", "UNIV", f"Excluded symbols: {len(excluded)}")
    append_log("INFO", "UNIV", f"To scan: {len(candidates)}")

    hist = _download_cached(candidates)
    if hist.empty:
        append_log("WARN", "UNIV", "dynamic universe fetch failed")
        return {
            "candidates_loaded": len(candidates_all),
            "excluded": len(excluded),
            "to_scan": len(candidates),
            "scored": 0,
            "errors": 0,
            "selected": [],
            "sector_leaders": [],
            "top_ranked": [],
            "write_path": "",
        }

    nifty = _download_nifty_cached(period="3mo", interval="1d")
    nifty_close = nifty["Close"].astype(float) if isinstance(nifty, pd.DataFrame) and "Close" in nifty.columns and not nifty.empty else pd.Series(dtype=float)
    nifty_20d_return = 0.0
    if len(nifty_close) > 20:
        c0 = float(nifty_close.iloc[-21])
        c1 = float(nifty_close.iloc[-1])
        nifty_20d_return = ((c1 - c0) / c0) if c0 > 0 else 0.0

    rows, errors = [], 0
    lvl0 = set(hist.columns.get_level_values(0)) if isinstance(hist.columns, pd.MultiIndex) else set()
    for sym in candidates:
        tk = f"{sym}.NS"
        try:
            if tk not in lvl0:
                continue
            met = _stock_metrics(sym, hist[tk], nifty_20d_return)
            if met:
                rows.append(met)
        except Exception:
            errors += 1
            continue

    append_log("INFO", "UNIV", f"Stocks scored: {len(rows)}")
    append_log("INFO", "UNIV", f"Errors: {errors}")
    if not rows:
        return {
            "candidates_loaded": len(candidates_all),
            "excluded": len(excluded),
            "to_scan": len(candidates),
            "scored": 0,
            "errors": errors,
            "selected": [],
            "sector_leaders": [],
            "top_ranked": [],
            "write_path": "",
        }

    sector_scores = _sector_rotation_scores(rows)

    for r in rows:
        sec_score = float(sector_scores.get(r["sector"], 0.0))
        r["final_score"] = (
            0.35 * r["trend_score"] +
            0.25 * r["rs_score"] +
            0.20 * r["volume_score"] +
            0.10 * r["atr_score"] +
            0.10 * sec_score
        )

    rows.sort(key=lambda x: x["final_score"], reverse=True)

    sector_count = defaultdict(int)
    selected = []
    for r in rows:
        sec = r["sector"]
        if sector_count[sec] >= int(getattr(CFG, "SECTOR_MAX_IN_UNIVERSE", 3)):
            continue
        selected.append(r)
        sector_count[sec] += 1
        if len(selected) >= target_size:
            break

    append_log("INFO", "UNIV", "Top ranked stocks:")
    for r in rows[:10]:
        append_log("INFO", "UNIV", f"{r['symbol']} score={r['final_score']:.3f}")

    syms = [r["symbol"] for r in selected]
    live_path = save_universe(syms, getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))) if syms else ""
    append_log("INFO", "UNIV", f"Selected universe size: {len(syms)}")

    return {
        "candidates_loaded": len(candidates_all),
        "excluded": len(excluded),
        "to_scan": len(candidates),
        "scored": len(rows),
        "errors": errors,
        "selected": syms,
        "sector_leaders": sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)[:5],
        "top_ranked": rows[:10],
        "write_path": live_path,
    }


def build_dynamic_universe(target_size: int = None) -> List[str]:
    return list(build_dynamic_universe_details(target_size=target_size).get("selected") or [])


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
    snap = get_market_regime_snapshot()
    regime = snap.get("regime", "WEAK")
    append_log(
        "INFO",
        "MARKET",
        f"regime={regime} nifty={snap.get('nifty',0):.2f} ema20={snap.get('ema20',0):.2f} "
        f"change1d={snap.get('chg1',0):.2f}% change5d={snap.get('chg5',0):.2f}%",
    )
    if regime == "WEAK":
        append_log("WARN", "MARKET", "regime=WEAK → skipping new BUY entries")
        return False
    return True
