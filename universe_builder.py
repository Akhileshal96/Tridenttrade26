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


def _download_cached(symbols: List[str], period="1y", interval="1d") -> pd.DataFrame:
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


def _download(symbols: List[str], period="1y", interval="1d") -> pd.DataFrame:
    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(tickers=tickers, period=period, interval=interval, auto_adjust=False, progress=False, threads=False, group_by="ticker")
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _is_valid_num(x) -> bool:
    try:
        v = float(x)
        return pd.notna(v) and v == v and abs(v) != float("inf")
    except Exception:
        return False


def _stock_metrics(sym: str, sdf: pd.DataFrame, nifty_20d_return: float | None) -> tuple[dict | None, str | None]:
    try:
        close = sdf["Close"].astype(float) if "Close" in sdf else pd.Series(dtype=float)
        high = sdf["High"].astype(float) if "High" in sdf else pd.Series(dtype=float)
        low = sdf["Low"].astype(float) if "Low" in sdf else pd.Series(dtype=float)
        vol = sdf["Volume"].astype(float) if "Volume" in sdf else pd.Series(dtype=float)

        if len(close) < 200:
            return None, "insufficient candles"

        price = float(close.iloc[-1]) if len(close) else 0.0
        prev_close = float(close.iloc[-2]) if len(close) > 1 else price
        close_20d = float(close.iloc[-21]) if len(close) > 20 else None

        sma200_s = close.rolling(200).mean() if len(close) >= 200 else pd.Series(dtype=float)
        sma200 = float(sma200_s.iloc[-1]) if len(sma200_s) else None
        trend_score = (price / sma200) if (_is_valid_num(price) and _is_valid_num(sma200) and float(sma200) > 0) else None
        if trend_score is None:
            return None, "missing SMA200"

        rs_score = None
        if close_20d is not None and _is_valid_num(close_20d) and float(close_20d) > 0 and _is_valid_num(nifty_20d_return):
            stock_return_20d = (price - close_20d) / close_20d
            rs_score = stock_return_20d - float(nifty_20d_return)

        volume_score = None
        if len(vol) >= 20:
            avg_vol_20 = float(vol.tail(20).mean())
            vol_today = float(vol.iloc[-1])
            if _is_valid_num(avg_vol_20) and avg_vol_20 > 0 and _is_valid_num(vol_today):
                volume_score = (vol_today / avg_vol_20)

        atr_score = None
        if len(high) >= 14 and len(low) >= 14 and _is_valid_num(price) and price > 0:
            tr = (high - low).abs()
            atr14 = float(tr.tail(14).mean()) if len(tr) >= 14 else float(tr.mean())
            if _is_valid_num(atr14):
                atr_score = atr14 / price

        pct_change = ((price - prev_close) / prev_close) if (_is_valid_num(prev_close) and prev_close > 0) else 0.0
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
        }, None
    except Exception:
        return None, "insufficient candles"


def _sector_rotation_scores(rows: List[dict]) -> Dict[str, float]:
    bucket = defaultdict(list)
    for r in rows:
        bucket[r["sector"]].append(r)

    def _avg_valid(arr, key):
        vals = [float(x.get(key)) for x in arr if _is_valid_num(x.get(key))]
        return float(sum(vals) / len(vals)) if vals else 0.0

    sector_scores = {}
    for sec, arr in bucket.items():
        avg_rs = _avg_valid(arr, "rs_score")
        avg_vol = _avg_valid(arr, "volume_score")
        avg_5d = _avg_valid(arr, "pct_change")
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

    skip_counts = {
        "insufficient candles": 0,
        "missing SMA200": 0,
        "missing ATR": 0,
        "missing relative strength": 0,
        "missing volume expansion": 0,
        "missing sector score": 0,
        "invalid final score": 0,
    }

    lookback_period = str(getattr(CFG, "UNIVERSE_LOOKBACK_PERIOD", "1y") or "1y")
    hist = _download_cached(candidates, period=lookback_period, interval="1d")
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
            "scoring_mode": "TREND_ONLY",
        }

    nifty = _download_nifty_cached(period="3mo", interval="1d")
    nifty_close = nifty["Close"].astype(float) if isinstance(nifty, pd.DataFrame) and "Close" in nifty.columns and not nifty.empty else pd.Series(dtype=float)
    nifty_20d_return = None
    if len(nifty_close) > 20:
        c0 = float(nifty_close.iloc[-21])
        c1 = float(nifty_close.iloc[-1])
        if _is_valid_num(c0) and c0 > 0 and _is_valid_num(c1):
            nifty_20d_return = ((c1 - c0) / c0)

    rows, errors = [], 0
    lvl0 = set(hist.columns.get_level_values(0)) if isinstance(hist.columns, pd.MultiIndex) else set()
    for sym in candidates:
        tk = f"{sym}.NS"
        try:
            if tk not in lvl0:
                skip_counts["insufficient candles"] += 1
                continue
            met, reason = _stock_metrics(sym, hist[tk], nifty_20d_return)
            if met:
                rows.append(met)
            elif reason in skip_counts:
                skip_counts[reason] += 1
        except Exception:
            errors += 1
            skip_counts["insufficient candles"] += 1
            continue

    if not rows:
        append_log("INFO", "UNIV", f"Stocks scored: 0")
        append_log("INFO", "UNIV", f"Errors: {errors}")
        append_log("INFO", "UNIV", f"Skipped due to candles: {skip_counts['insufficient candles']}")
        append_log("INFO", "UNIV", f"Skipped due to ATR missing: {skip_counts['missing ATR']}")
        append_log("INFO", "UNIV", f"Skipped due to RS missing: {skip_counts['missing relative strength']}")
        append_log("INFO", "UNIV", f"Skipped due to invalid final score: {skip_counts['invalid final score']}")
        append_log("INFO", "UNIV", "Scoring mode used: TREND_ONLY")
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
            "scoring_mode": "TREND_ONLY",
        }

    sector_scores = _sector_rotation_scores(rows)

    scored_rows = []
    mode_counts = {"FULL": 0, "PARTIAL": 0, "TREND_ONLY": 0}
    for r in rows:
        trend = r.get("trend_score")
        rs = r.get("rs_score")
        vol = r.get("volume_score")
        atr = r.get("atr_score")
        sec_score = sector_scores.get(r.get("sector"))

        final = None
        mode = None
        full_ok = all(_is_valid_num(x) for x in [trend, rs, vol, atr, sec_score])
        if full_ok:
            mode = "FULL"
            final = 0.35 * float(trend) + 0.25 * float(rs) + 0.20 * float(vol) + 0.10 * float(atr) + 0.10 * float(sec_score)
        elif _is_valid_num(trend) and _is_valid_num(rs):
            mode = "PARTIAL"
            final = 0.65 * float(trend) + 0.35 * float(rs)
            if not _is_valid_num(vol):
                skip_counts["missing volume expansion"] += 1
            if not _is_valid_num(atr):
                skip_counts["missing ATR"] += 1
            if not _is_valid_num(sec_score):
                skip_counts["missing sector score"] += 1
        elif _is_valid_num(trend):
            mode = "TREND_ONLY"
            final = float(trend)
            if not _is_valid_num(rs):
                skip_counts["missing relative strength"] += 1
        else:
            skip_counts["missing SMA200"] += 1
            continue

        if not _is_valid_num(final):
            skip_counts["invalid final score"] += 1
            continue

        r2 = dict(r)
        r2["final_score"] = float(final)
        r2["score_mode"] = mode
        mode_counts[mode] += 1
        scored_rows.append(r2)

    # If too few scored names, fallback to trend-only ranking from available trend metrics.
    scoring_mode_used = "FULL"
    if len(scored_rows) < 10:
        trend_rows = []
        for r in rows:
            tr = r.get("trend_score")
            if _is_valid_num(tr):
                x = dict(r)
                x["final_score"] = float(tr)
                x["score_mode"] = "TREND_ONLY"
                trend_rows.append(x)
        if trend_rows:
            scored_rows = trend_rows
            mode_counts = {"FULL": 0, "PARTIAL": 0, "TREND_ONLY": len(scored_rows)}
            scoring_mode_used = "TREND_ONLY"

    if scoring_mode_used != "TREND_ONLY":
        if mode_counts["PARTIAL"] > 0 or mode_counts["TREND_ONLY"] > 0:
            scoring_mode_used = "PARTIAL"
        else:
            scoring_mode_used = "FULL"

    scored_rows.sort(key=lambda x: x["final_score"], reverse=True)

    sector_count = defaultdict(int)
    selected = []
    for r in scored_rows:
        sec = r["sector"]
        if sector_count[sec] >= int(getattr(CFG, "SECTOR_MAX_IN_UNIVERSE", 3)):
            continue
        selected.append(r)
        sector_count[sec] += 1
        if len(selected) >= target_size:
            break

    append_log("INFO", "UNIV", f"Stocks scored: {len(scored_rows)}")
    append_log("INFO", "UNIV", f"Errors: {errors}")
    append_log("INFO", "UNIV", f"Skipped due to candles: {skip_counts['insufficient candles']}")
    append_log("INFO", "UNIV", f"Skipped due to ATR missing: {skip_counts['missing ATR']}")
    append_log("INFO", "UNIV", f"Skipped due to RS missing: {skip_counts['missing relative strength']}")
    append_log("INFO", "UNIV", f"Skipped due to invalid final score: {skip_counts['invalid final score']}")
    append_log("INFO", "UNIV", f"Scoring mode used: {scoring_mode_used}")

    append_log("INFO", "UNIV", "Top ranked stocks:")
    for r in scored_rows[:10]:
        append_log("INFO", "UNIV", f"{r['symbol']} score={r['final_score']:.3f}")

    syms = [r["symbol"] for r in selected]
    live_path = save_universe(syms, getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))) if syms else ""
    append_log("INFO", "UNIV", f"Selected universe size: {len(syms)}")

    return {
        "candidates_loaded": len(candidates_all),
        "excluded": len(excluded),
        "to_scan": len(candidates),
        "scored": len(scored_rows),
        "errors": errors,
        "selected": syms,
        "sector_leaders": sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)[:5],
        "top_ranked": scored_rows[:10],
        "write_path": live_path,
        "scoring_mode": scoring_mode_used,
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
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    valid = bool(snap.get("valid_data", False))

    if not valid:
        append_log("WARN", "MARKET", "NIFTY data unavailable")
        if regime == "UNKNOWN":
            append_log("WARN", "MARKET", "regime=UNKNOWN using fallback")
    else:
        append_log(
            "INFO",
            "MARKET",
            f"regime={regime} nifty={snap.get('nifty',0):.2f} ema20={snap.get('ema20',0):.2f} "
            f"change1d={snap.get('chg1',0):.2f}% change5d={snap.get('chg5',0):.2f}%",
        )

    if regime == "WEAK":
        append_log("WARN", "MARKET", "regime=WEAK → skipping new BUY entries")
        return False

    if regime == "UNKNOWN":
        block_on_unknown = bool(getattr(CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", False))
        if block_on_unknown:
            append_log("WARN", "MARKET", "regime=UNKNOWN and BLOCK_ON_UNKNOWN_MARKET_REGIME=true → skipping new BUY entries")
            return False
    return True
