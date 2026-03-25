import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log
from strategy_engine import generate_signal, generate_mean_reversion_signal, generate_vwap_ema_signal, generate_pullback_signal
from excluded_store import load_excluded, add_symbol, remove_symbol
from execution_engine import monitor_positions as ee_monitor_positions, process_entries as ee_process_entries, force_exit_all as ee_force_exit_all
import risk_engine as RISK
import research_engine as RE
from universe_builder import SECTOR_MAP
from market_regime import get_market_regime_snapshot, get_regime_entry_mode
import strategy_analytics as SA
from state_lock import STATE_LOCK, safe_update, PositionManager

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "positions": {},  # SYMBOL -> trade dict
    "today_pnl": 0.0,
    "day_key": datetime.now(IST).strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
    "wallet_net_inr": 0.0,
    "wallet_available_inr": 0.0,
    "last_wallet": float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0),
    "daily_loss_cap_inr": float(getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0)),
    "daily_profit_milestone_inr": float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 90.0)),
    "profit_milestone_hit": False,
    "last_wallet_sync_ts": None,
    "cooldown_until": None,
    "last_exit_ts": {},
    "skip_cooldown": {},
    "loss_streak": 0,
    "reduce_size_factor": 1.0,
    "pause_entries_until": None,
    "halt_for_day": False,
    "day_peak_pnl": 0.0,
    "sector_map_cache": None,
    "research_universe": [],
    "fallback_universe": [],
    "active_universe": [],
    "active_universe_last_refresh": None,
    "active_no_setup_cycles": 0,
    "opening_mode": "OPEN_CLEAN",
    "opening_metrics": {},
    "open_feed_retry_count": 0,
    "mean_reversion_dry_cycles": 0,
    "entry_tier_for_cooldown": None,
    "no_entry_cycles": 0,
    "top3_dry_cycles": 0,
    "fallback_mode_active": False,
    "active_strategy_families": [],
    "active_strategy_last_refresh": None,
    "active_strategy_last_reason": "",
    "strategy_scores_last": {},
    "strategy_selection_history": [],
    "last_route_universe_source": "n/a",
    "last_trend_direction": "UNKNOWN",
    "eod_report_sent_date": "",
    "confirm_strictness": "STRICT",
    "signals_seen_window": 0,
    "entries_executed_window": 0,
    "signal_event_ts": [],
    "entry_event_ts": [],
    "micro_mode_active": False,
    "micro_mode_trade_count": 0,
    "micro_mode_regime": "",
    "realized_today": 0.0,
    "unrealized_now": 0.0,
    "pnl_so_far": 0.0,
    "trade_events": [],
    "research_events": [],
    "universe_changes_today": [],
    "route_changes_today": [],
    "recent_entries": [],
    "recent_exits": [],
}

# backwards compatibility for any caller that still checks open_trades key
STATE["open_trades"] = STATE["positions"]
PM = PositionManager(STATE)

RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(getattr(CFG, "MAX_ENTRY_SLIPPAGE_PCT", 0.30)),
    "BUCKET_MODE": str(getattr(CFG, "BUCKET_MODE", "PCT")).upper(),
    "BUCKET_PCT": float(getattr(CFG, "BUCKET_PCT", 10.0)),
    "BUCKET_INR": float(getattr(CFG, "BUCKET_INR", 1000.0)),
    "BUCKET_MIN_INR": float(getattr(CFG, "BUCKET_MIN_INR", 1000.0)),
    "BUCKET_MAX_INR": float(getattr(CFG, "BUCKET_MAX_INR", 5000.0)),
    "MAX_EXPOSURE_PCT": float(getattr(CFG, "MAX_EXPOSURE_PCT", 60.0)),
    "USE_BUCKET_SLABS": bool(getattr(CFG, "USE_BUCKET_SLABS", True)),
    "SOFT_PROFIT_TARGET": str(os.getenv("SOFT_PROFIT_TARGET", "true")).strip().lower() == "true",
}

STRATEGY_REGISTRY = [
    {
        "family": "trend_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "TRENDING"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_signal",
        "class": "primary",
    },
    {
        "family": "pullback_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "TRENDING"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_pullback_signal",
        "class": "primary",
    },
    {
        "family": "short_breakdown",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE", "OPEN_UNSAFE"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "primary",
    },
    {
        "family": "mean_reversion",
        "direction": "long",
        "preferred_regimes": ["SIDEWAYS", "UNKNOWN"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_mean_reversion_signal",
        "class": "primary",
    },
    {
        "family": "fallback_long",
        "direction": "long",
        "preferred_regimes": ["UNKNOWN", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_mean_reversion_signal",
        "class": "fallback",
    },
    {
        "family": "fallback_short",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN", "VOLATILE", "UNKNOWN"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "fallback",
    },
    {
        "family": "outlier_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_vwap_ema_signal",
        "class": "outlier",
    },
    {
        "family": "outlier_short",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE", "OPEN_UNSAFE"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "outlier",
    },
]

_NOTIFIER = None


def set_notifier(fn):
    global _NOTIFIER
    _NOTIFIER = fn


def _notify(msg: str):
    if not _NOTIFIER:
        return
    try:
        _NOTIFIER(msg)
    except Exception as e:
        append_log("WARN", "NOTIFY", f"Notifier error: {e}")


def _append_runtime_event(bucket: str, payload: dict, limit: int = 300):
    if not isinstance(payload, dict):
        return
    ts = datetime.now(IST).isoformat(timespec="seconds")
    rec = {"ts": ts}
    rec.update(payload)
    events = STATE.setdefault(bucket, [])
    if not isinstance(events, list):
        events = []
        STATE[bucket] = events
    events.append(rec)
    if len(events) > limit:
        del events[:-limit]


def _log_trade_event(event_tag: str, trade: dict):
    tr = dict(trade or {})
    sym = str(tr.get("symbol") or "").strip().upper()
    side = str(tr.get("side") or "BUY").upper()
    fam = str(tr.get("strategy_family") or "unknown")
    tier = str(tr.get("confidence_tier") or "n/a")
    qty = int(tr.get("qty") or tr.get("quantity") or 0)
    entry = float(tr.get("entry") or tr.get("entry_price") or 0.0)
    exit_px = float(tr.get("exit_price") or tr.get("exit") or 0.0) if tr.get("exit_price") is not None or tr.get("exit") is not None else None
    oid = tr.get("order_id")
    entry_ts = str(tr.get("entry_time") or "-")
    exit_ts = str(tr.get("exit_time") or "-")
    exit_reason = str(tr.get("exit_reason") or tr.get("reason") or "-")
    parts = [
        f"symbol={sym}",
        f"side={side}",
        f"family={fam}",
        f"tier={tier}",
        f"qty={qty}",
        f"entry={entry:.2f}",
        f"order_id={oid or '-'}",
        f"entry_ts={entry_ts}",
    ]
    if exit_px is not None:
        parts.append(f"exit={float(exit_px):.2f}")
    parts.append(f"exit_ts={exit_ts}")
    parts.append(f"exit_reason={exit_reason}")
    append_log("INFO", event_tag, " ".join(parts))
    _append_runtime_event("trade_events", {"event": event_tag, **tr})


def _positions():
    pos = STATE.setdefault("positions", {})

    def _normalize_trade_dict(sym: str, tr: dict):
        if not sym or not isinstance(tr, dict):
            return None
        entry = float(tr.get("entry") or tr.get("entry_price") or 0.0)
        qty = int(tr.get("qty") or tr.get("quantity") or 1)
        peak = float(tr.get("peak") or tr.get("peak_pct") or 0.0)
        trail_active = bool(tr.get("trail_active", tr.get("trailing_active", False)))
        peak_pnl_inr = float(tr.get("peak_pnl_inr") or 0.0)
        return {
            "entry": entry,
            "entry_price": entry,
            "qty": qty,
            "quantity": qty,
            "peak": peak,
            "peak_pct": peak,
            "peak_pnl_inr": peak_pnl_inr,
            "trail_active": trail_active,
            "trailing_active": trail_active,
            "order_id": tr.get("order_id"),
        }

    # backward compatibility: merge legacy multi-trade map if present
    legacy_map = STATE.get("open_trades")
    if isinstance(legacy_map, dict) and legacy_map is not pos:
        migrated = 0
        for raw_sym, tr in legacy_map.items():
            sym = str(raw_sym or "").strip().upper()
            if not sym or sym in pos:
                continue
            norm = _normalize_trade_dict(sym, tr)
            if norm:
                pos[sym] = norm
                migrated += 1
        if migrated:
            append_log("INFO", "STATE", f"Merged {migrated} legacy open_trades into positions")

    # backward compatibility: migrate legacy single trade slot if present
    legacy = STATE.get("open_trade")
    if legacy and isinstance(legacy, dict):
        sym = str(legacy.get("symbol") or "").strip().upper()
        if sym and sym not in pos:
            norm = _normalize_trade_dict(sym, legacy)
            if norm:
                pos[sym] = norm
                append_log("INFO", "STATE", f"Migrated legacy open_trade -> positions for {sym}")

    # ensure trailing keys exist for current runtime positions
    for tr in pos.values():
        if not isinstance(tr, dict):
            continue
        tr.setdefault("peak_pnl_inr", 0.0)
        tr.setdefault("trail_active", bool(tr.get("trailing_active", False)))
        tr.setdefault("trailing_active", bool(tr.get("trail_active", False)))

    # keep alias aligned
    STATE["open_trades"] = pos
    return pos


def _trade_entry_qty(trade: dict) -> tuple[float, int]:
    entry = float((trade or {}).get("entry_price") or (trade or {}).get("entry") or 0.0)
    qty = int((trade or {}).get("quantity") or (trade or {}).get("qty") or 0)
    return entry, (qty if qty > 0 else 1)


def _parse_hhmm(s):
    try:
        hh, mm = str(s).strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0




def _load_sector_map():
    mp = {}
    path = os.getenv("SECTOR_MAP_PATH", os.path.join(DATA_DIR, "sector_map.csv"))
    if not os.path.exists(path):
        return mp
    try:
        with open(path, "r") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or "," not in ln:
                    continue
                sym, sec = ln.split(",", 1)
                sym = sym.strip().upper()
                sec = sec.strip().upper()
                if sym and sec:
                    mp[sym] = sec
    except Exception:
        return {}
    return mp


def _sector_for_symbol(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return "UNKNOWN"
    mp = STATE.setdefault("sector_map_cache", None)
    if mp is None:
        mp = _load_sector_map()
        STATE["sector_map_cache"] = mp
    return mp.get(sym, "UNKNOWN")
def _past_force_exit_time():
    now = datetime.now(IST)
    fh, fm = _parse_hhmm(getattr(CFG, "FORCE_EXIT", "15:10"))
    cutoff = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return now >= cutoff


def _ensure_day_key():
    with STATE_LOCK:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if STATE.get("day_key") != today:
            STATE["day_key"] = today
            STATE["today_pnl"] = 0.0
            _positions().clear()
            STATE["profit_milestone_hit"] = False
            STATE["cooldown_until"] = None
            STATE["loss_streak"] = 0
            STATE["reduce_size_factor"] = 1.0
            STATE["pause_entries_until"] = None
            STATE["halt_for_day"] = False
            STATE["day_peak_pnl"] = 0.0
            STATE["sector_map_cache"] = None
            STATE["open_feed_retry_count"] = 0
            STATE["top3_dry_cycles"] = 0
            STATE["active_strategy_families"] = []
            STATE["active_strategy_last_refresh"] = None
            STATE["active_strategy_last_reason"] = ""
            STATE["strategy_scores_last"] = {}
            STATE["strategy_selection_history"] = []
            STATE["last_route_universe_source"] = "n/a"
            STATE["last_trend_direction"] = "UNKNOWN"
            append_log("INFO", "DAY", f"Auto rollover reset for {today}")


def set_runtime_param(key, value):
    RUNTIME[key] = value


def manual_reset_day():
    with STATE_LOCK:
        STATE["today_pnl"] = 0.0
        _positions().clear()
        STATE["day_key"] = datetime.now(IST).strftime("%Y-%m-%d")
        STATE["profit_milestone_hit"] = False
        STATE["cooldown_until"] = None
        STATE["loss_streak"] = 0
        STATE["reduce_size_factor"] = 1.0
        STATE["pause_entries_until"] = None
        STATE["halt_for_day"] = False
        STATE["day_peak_pnl"] = 0.0
        STATE["sector_map_cache"] = None
        STATE["top3_dry_cycles"] = 0
        STATE["active_strategy_families"] = []
        STATE["active_strategy_last_refresh"] = None
        STATE["active_strategy_last_reason"] = ""
        STATE["strategy_scores_last"] = {}
        STATE["strategy_selection_history"] = []
        STATE["last_route_universe_source"] = "n/a"
        STATE["last_trend_direction"] = "UNKNOWN"
    append_log("INFO", "DAY", "Manual day reset executed")
    return True


def is_live_enabled():
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))



def list_exclusions():
    s = load_excluded()
    if not s:
        return "✅ Excluded symbols: (none)"
    return "⛔ Excluded symbols:\n" + "\n".join(sorted(s))


def exclude_symbol(sym):
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /exclude SYMBOL"
    changed = add_symbol(sym)
    if changed:
        append_log("WARN", "EXCL", f"Excluded {sym}")
    return f"⛔ {sym} excluded permanently. (/include {sym} to release)"


def include_symbol(sym):
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /include SYMBOL"
    changed = remove_symbol(sym)
    if changed:
        append_log("INFO", "EXCL", f"Included back {sym}")
        return f"✅ {sym} released from exclusions."
    return f"ℹ️ {sym} was not in exclusions."

def _atomic_copy(src, dst):
    if not os.path.exists(src):
        return False
    ddir = os.path.dirname(dst)
    if ddir:
        os.makedirs(ddir, exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "r") as fsrc, open(tmp, "w") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp, dst)
    return True


def _load_universe_from(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


def load_universe_trading():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))

    if not os.path.exists(trade_path) and os.path.exists(live_path):
        _atomic_copy(live_path, trade_path)
        append_log("INFO", "PROMOTE", "Bootstrapped trading universe from live universe")

    syms = _load_universe_from(trade_path)
    excl = load_excluded()
    syms = [s for s in syms if s not in excl]

    try:
        syms = syms[: int(getattr(CFG, "UNIVERSE_SIZE", 30))]
    except Exception:
        pass
    return syms


def load_universe_live():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = load_excluded()
    return [s for s in syms if s not in excl]


def _parse_windows(win_str):
    windows = []
    if not win_str:
        return windows
    for p in [x.strip() for x in str(win_str).split(",") if x.strip()]:
        if "-" not in p:
            continue
        a, b = p.split("-", 1)
        windows.append((_parse_hhmm(a), _parse_hhmm(b)))
    return windows


def _in_any_promote_window():
    now = datetime.now(IST)
    for (ah, am), (bh, bm) in _parse_windows(getattr(CFG, "PROMOTE_WINDOWS", "")):
        start = now.replace(hour=ah, minute=am, second=0, microsecond=0)
        end = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        if start <= now <= end:
            return True
    return False


def _cooldown_ok():
    cd_min = float(getattr(CFG, "PROMOTE_COOLDOWN_MIN", 60))
    last = STATE.get("last_promote_ts")
    if not last:
        return True
    return (datetime.now(IST) - last) >= timedelta(minutes=cd_min)


def _top10_overlap_ratio(a, b):
    a10, b10 = a[:10], b[:10]
    if not a10 or not b10:
        return 0.0
    inter = len(set(a10).intersection(set(b10)))
    return float(inter) / float(min(len(a10), len(b10)))


def _market_stable():
    try:
        sym = getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES").strip().upper()
        token = token_for_symbol(sym)
        kite = get_kite()
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=5)
        data = kite.historical_data(token, from_dt, to_dt, "15minute")
        time.sleep(0.3)

        df = pd.DataFrame(data)
        if df.empty or not all(c in df.columns for c in ["high", "low", "close"]):
            return False
        tail = df.tail(10)
        if len(tail) < 8:
            return False
        rng_pct = ((tail["high"] - tail["low"]).astype(float) / tail["close"].astype(float)) * 100.0
        return float(rng_pct.mean()) <= float(getattr(CFG, "STABILITY_ATR_PCT_MAX", 0.35))
    except Exception as e:
        append_log("WARN", "STABLE", f"Stability check failed: {e}")
        return False


def promote_universe(reason="AUTO"):
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))
    live = _load_universe_from(live_path)
    trade = _load_universe_from(trade_path)
    if not live:
        STATE["last_promote_msg"] = "No live universe available"
        return False

    min_overlap = float(getattr(CFG, "PROMOTE_TOP10_OVERLAP_MIN", 0.60))
    overlap = _top10_overlap_ratio(live, trade) if trade else 1.0
    if trade and overlap < min_overlap:
        STATE["last_promote_msg"] = f"Blocked (overlap {overlap:.2f} < {min_overlap:.2f})"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
        return False

    ok = _atomic_copy(live_path, trade_path)
    if ok:
        STATE["last_promote_ts"] = datetime.now(IST)
        STATE["last_promote_msg"] = f"Promoted ({reason}) overlap={overlap:.2f}"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
    return ok


def _is_market_hours(now: datetime) -> bool:
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end


def _cached_wallet_value() -> float:
    cached = float(STATE.get("last_wallet") or 0.0)
    if cached > 0:
        return cached
    return float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)


def _sync_wallet_and_caps(force=False):
    now = datetime.now(IST)
    last = STATE.get("last_wallet_sync_ts")

    day_interval = int(getattr(CFG, "WALLET_SYNC_INTERVAL_SEC", 120))
    night_interval = int(getattr(CFG, "WALLET_NIGHT_SYNC_INTERVAL_SEC", 900))
    in_market = _is_market_hours(now)
    min_interval = day_interval if in_market else night_interval

    if not force and last and (now - last) < timedelta(seconds=min_interval):
        if not in_market:
            cached = _cached_wallet_value()
            STATE["wallet_net_inr"] = max(0.0, cached)
            STATE["wallet_available_inr"] = max(0.0, cached)
            append_log("INFO", "WALLET", "Night skip → cached wallet used")
        return

    retries = max(1, int(getattr(CFG, "WALLET_SYNC_RETRIES", 3)))
    backoff = float(getattr(CFG, "WALLET_RETRY_BASE_SEC", 1.5))

    wallet_net = _cached_wallet_value()
    wallet_avail = wallet_net
    synced = False

    for attempt in range(retries):
        try:
            m = get_kite().margins() or {}
            eq = m.get("equity", {}) if isinstance(m, dict) else {}
            wallet_net = float(eq.get("net") or wallet_net or 0.0)
            avail = eq.get("available", {}) if isinstance(eq, dict) else {}
            if isinstance(avail, dict):
                wallet_avail = float(
                    avail.get("live_balance") or avail.get("cash") or avail.get("opening_balance") or avail.get("adhoc_margin") or wallet_net
                )
            else:
                wallet_avail = wallet_net
            STATE["last_wallet"] = max(0.0, wallet_net)
            append_log("INFO", "WALLET", f"Synced wallet={wallet_net:.2f}")
            synced = True
            break
        except Exception as e:
            append_log("WARNING", "WALLET", f"Attempt {attempt + 1} failed: {e}")
            if attempt + 1 < retries:
                append_log("WARNING", "WALLET", f"Retry {attempt + 2}/{retries}")
                time.sleep(backoff * (attempt + 1))

    if not synced:
        wallet_net = _cached_wallet_value()
        wallet_avail = wallet_net
        append_log("WARNING", "WALLET", f"API failure → using cached wallet={wallet_net:.2f}")

    if wallet_net <= 0:
        wallet_net = float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        wallet_avail = max(wallet_avail, wallet_net)
        append_log("WARNING", "WALLET", f"API failed → using cached wallet={wallet_net:.2f}")

    STATE["wallet_net_inr"] = max(0.0, wallet_net)
    STATE["wallet_available_inr"] = max(0.0, wallet_avail if wallet_avail > 0 else wallet_net)

    loss_pct = float(os.getenv("DAILY_LOSS_CAP_PCT", "2.0"))
    prof_pct = float(os.getenv("DAILY_PROFIT_MILESTONE_PCT", os.getenv("DAILY_PROFIT_TARGET_PCT", "1.0")))
    if STATE["wallet_net_inr"] > 0:
        STATE["daily_loss_cap_inr"] = STATE["wallet_net_inr"] * loss_pct / 100.0
        STATE["daily_profit_milestone_inr"] = STATE["wallet_net_inr"] * prof_pct / 100.0
    else:
        STATE["daily_loss_cap_inr"] = float(getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0))
        STATE["daily_profit_milestone_inr"] = float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 90.0))
    STATE["last_wallet_sync_ts"] = now


def _open_positions_count():
    return len(_positions())


def _current_exposure_inr():
    total = 0.0
    for t in _positions().values():
        e, q = _trade_entry_qty(t)
        total += e * q
    return total


def _max_exposure_inr():
    base = float(STATE.get("wallet_net_inr") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    return base * float(RUNTIME.get("MAX_EXPOSURE_PCT", 60.0)) / 100.0


def _bucket_inr(wallet_net: float) -> float:
    if bool(RUNTIME.get("USE_BUCKET_SLABS", True)):
        if wallet_net < 5000:
            return 500.0
        if wallet_net <= 15000:
            return 5000.0
        if wallet_net <= 30000:
            return 7000.0
        if wallet_net <= 60000:
            return 10000.0
        if wallet_net <= 100000:
            return 15000.0
        return 20000.0

    mode = str(RUNTIME.get("BUCKET_MODE", "PCT")).upper()
    if mode == "PCT":
        bucket = wallet_net * float(RUNTIME.get("BUCKET_PCT", 10.0)) / 100.0
    else:
        bucket = float(RUNTIME.get("BUCKET_INR", 1000.0))
    bmin = float(RUNTIME.get("BUCKET_MIN_INR", 1000.0))
    bmax = float(RUNTIME.get("BUCKET_MAX_INR", 5000.0))
    return max(bmin, min(bucket, bmax))


def _calc_qty(symbol: str, price: float):
    wallet = float(STATE.get("wallet_net_inr") or 0.0)
    if wallet <= 0:
        wallet = float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        append_log("WARNING", "BUCKET", "Wallet unavailable → fallback capital used")
    qty, bucket, bucket_qty, risk_qty = RISK.get_position_size(price, wallet)
    size_factor = float(STATE.get("reduce_size_factor") or 1.0)
    if size_factor < 1.0:
        qty = int(qty * max(0.1, size_factor))
    qty = qty if qty >= 1 else 0
    append_log("INFO", "BUCKET", f"wallet={wallet:.2f} slab_bucket={bucket:.2f} exposure={_current_exposure_inr():.2f}/{_max_exposure_inr():.2f}")
    append_log("INFO", "SIZE", f"{symbol} price={price:.2f} qty={qty} bucket_qty={bucket_qty} risk_qty={risk_qty}")
    return qty, bucket_qty, risk_qty


def _ltp(kite, sym):
    try:
        ins = f"{CFG.EXCHANGE}:{sym}"
        return float(kite.ltp([ins])[ins]["last_price"])
    except Exception:
        return None


def _place_live_order(kite, sym, side, qty):
    try:
        return kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=CFG.EXCHANGE,
            tradingsymbol=sym,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=CFG.PRODUCT,
            order_type=kite.ORDER_TYPE_MARKET,
        )
    except Exception as e:
        append_log("ERROR", "ORDER", f"Order failed {sym} {side} qty={qty}: {e}")
        return None


def _set_cooldown():
    sec = int(getattr(CFG, "COOLDOWN_SECONDS", 120))
    # Slightly relax cooldown after 09:30 for repeated valid MICRO/REDUCED entries.
    tier = str(STATE.get("entry_tier_for_cooldown") or "").upper()
    now = datetime.now(IST)
    post_0930 = now.time() >= dt_time(9, 30)
    if post_0930 and tier in ("MICRO", "REDUCED"):
        streak_key = "post0930_valid_tier_streak"
        streak = int(STATE.get(streak_key) or 0) + 1
        STATE[streak_key] = streak
        if streak >= 2:
            sec = max(60, int(round(sec * 0.75)))
            append_log("INFO", "RISK", f"cooldown_refined tier={tier} streak={streak} sec={sec}")
    else:
        STATE["post0930_valid_tier_streak"] = 0
    STATE["cooldown_until"] = datetime.now(IST) + timedelta(seconds=sec)
    STATE["entry_tier_for_cooldown"] = None


def _apply_skip_cooldown(sym: str, reason: str, minutes: int = 3, side: str = "BUY", signal_price: float | None = None, strategy_tag: str = ""):
    sym = (sym or "").strip().upper()
    if not sym:
        return
    until = datetime.now(IST) + timedelta(minutes=max(1, int(minutes)))
    STATE.setdefault("skip_cooldown", {})[sym] = until
    append_log("INFO", "SKIP", f"{sym} cooldown applied reason={reason}")
    try:
        rec = {"symbol": sym, "side": str(side or "BUY").upper(), "reason": reason}
        if strategy_tag:
            rec["strategy_tag"] = strategy_tag
        if signal_price is not None:
            rec["signal_price"] = float(signal_price)
        SA.record_skipped_signal(rec)
    except Exception:
        pass


def _skip_cooldown_active(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    until = STATE.setdefault("skip_cooldown", {}).get(sym)
    if not until:
        return False
    if datetime.now(IST) >= until:
        STATE["skip_cooldown"].pop(sym, None)
        return False
    return True


def _close_position(sym, reason="MANUAL", ltp_override=None):
    sym = (sym or "").strip().upper()
    trade = _positions().get(sym)
    if not trade:
        return False
    entry, qty = _trade_entry_qty(trade)
    side = str(trade.get("side") or "LONG").upper()
    ltp = float(ltp_override) if ltp_override is not None else None

    if not is_live_enabled():
        if ltp is None:
            ltp = entry
        pnl, pnl_pct = _calc_pnl(entry, ltp, qty, side=side)
        safe_update(STATE, "today_pnl", lambda x: float(x or 0.0) + pnl)
        RISK.update_loss_streak(STATE, pnl)
        RISK.check_day_drawdown_guard(STATE)
        PM.remove(sym)
        STATE["last_exit_ts"][sym] = datetime.now(IST)
        _set_cooldown()
        exit_time = datetime.now(IST).isoformat(timespec="seconds")
        enriched = dict(trade)
        enriched.update({"symbol": sym, "qty": qty, "entry": entry, "exit_price": ltp, "exit_time": exit_time, "exit_reason": reason})
        _log_trade_event("CLOSE", enriched)
        _log_trade_event("TRADE", {**enriched, "pnl_inr": pnl, "pnl_pct": pnl_pct})
        append_log("WARN", "EXIT", f"{sym} family={trade.get('strategy_family','-')} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
        append_log("INFO", "RISK", f"symbol={sym} loss_streak={int(STATE.get('loss_streak') or 0)} halt_for_day={bool(STATE.get('halt_for_day'))}")
        _append_runtime_event("recent_exits", {"symbol": sym, "side": side, "qty": qty, "entry": entry, "exit": ltp, "reason": reason, "pnl_inr": pnl, "ts": exit_time}, limit=40)
        SA.record_trade_exit(
            {
                "entry_time": trade.get("entry_time") or "",
                "symbol": sym,
                "side": side,
                "qty": qty,
                "entry": entry,
                "exit": ltp,
                "pnl_inr": pnl,
                "pnl_pct": pnl_pct,
                "reason": reason,
                "strategy_tag": trade.get("strategy_tag") or "unknown",
                "strategy_family": trade.get("strategy_family") or "unknown",
                "market_regime": trade.get("market_regime") or "UNKNOWN",
                "universe_source": trade.get("universe_source") or "primary",
                "sector": trade.get("sector") or _sector_for_symbol(sym),
            }
        )
        exit_side = "BUY" if side == "SHORT" else "SELL"
        _notify(f"🔴 {exit_side} PAPER\nSymbol: {sym}\nExit: {ltp:.2f}\nPnL ₹: {pnl:.2f}\nPnL %: {pnl_pct:.2f}%\nReason: {reason}")
        return True

    kite = get_kite()
    oid = None
    close_side = "BUY" if side == "SHORT" else "SELL"
    for _ in range(3):
        oid = _place_live_order(kite, sym, close_side, qty)
        if oid:
            break
        time.sleep(0.6)
    if not oid:
        return False
    if ltp is None:
        ltp = _ltp(kite, sym) or entry

    pnl, pnl_pct = _calc_pnl(entry, ltp, qty, side=side)
    safe_update(STATE, "today_pnl", lambda x: float(x or 0.0) + pnl)
    RISK.update_loss_streak(STATE, pnl)
    RISK.check_day_drawdown_guard(STATE)
    PM.remove(sym)
    STATE["last_exit_ts"][sym] = datetime.now(IST)
    _set_cooldown()
    exit_time = datetime.now(IST).isoformat(timespec="seconds")
    enriched = dict(trade)
    enriched.update({"symbol": sym, "qty": qty, "entry": entry, "exit_price": ltp, "exit_time": exit_time, "exit_reason": reason})
    if oid:
        enriched["order_id"] = oid
    _log_trade_event("CLOSE", enriched)
    _log_trade_event("TRADE", {**enriched, "pnl_inr": pnl, "pnl_pct": pnl_pct})
    append_log("WARN", "EXIT", f"{sym} family={trade.get('strategy_family','-')} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
    append_log("INFO", "RISK", f"symbol={sym} loss_streak={int(STATE.get('loss_streak') or 0)} halt_for_day={bool(STATE.get('halt_for_day'))}")
    _append_runtime_event("recent_exits", {"symbol": sym, "side": side, "qty": qty, "entry": entry, "exit": ltp, "reason": reason, "pnl_inr": pnl, "ts": exit_time}, limit=40)
    SA.record_trade_exit(
        {
            "entry_time": trade.get("entry_time") or "",
            "symbol": sym,
            "side": side,
            "qty": qty,
            "entry": entry,
            "exit": ltp,
            "pnl_inr": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "strategy_tag": trade.get("strategy_tag") or "unknown",
            "strategy_family": trade.get("strategy_family") or "unknown",
            "market_regime": trade.get("market_regime") or "UNKNOWN",
            "universe_source": trade.get("universe_source") or "primary",
            "sector": trade.get("sector") or _sector_for_symbol(sym),
        }
    )
    _notify(f"🔴 {close_side} LIVE\nSymbol: {sym}\nExit: {ltp:.2f}\nPnL ₹: {pnl:.2f}\nPnL %: {pnl_pct:.2f}%\nReason: {reason}")
    return True


def _apply_strategy_allocation(qty: int, strategy_tag: str) -> int:
    mult, reason = SA.get_strategy_multiplier(strategy_tag, CFG)
    append_log("INFO", "ALLOC", f"strategy={strategy_tag} multiplier={mult:.2f} reason={reason}")
    return max(0, int(math.floor(max(0, qty) * max(0.0, mult))))


def _close_all_open_trades(reason="MANUAL"):
    return ee_force_exit_all(_positions(), _close_position, reason=reason)


def _within_entry_window():
    now = datetime.now(IST)
    sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
    eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def _can_open_new_trade(sym, entry, qty=1, momentum_positive=False):
    sym = sym.strip().upper()
    now = datetime.now(IST)

    with STATE_LOCK:
        cooldown_until = STATE.get("cooldown_until")
        last_exit = STATE.get("last_exit_ts", {}).get(sym)
        halt_for_day = bool(STATE.get("halt_for_day"))
        pause_until = STATE.get("pause_entries_until")
        wallet_avail = float(STATE.get("wallet_available_inr") or 0.0)
        wallet_net = float(STATE.get("wallet_net_inr") or 0.0)

    if cooldown_until and now < cooldown_until:
        append_log("INFO", "SKIP", f"{sym} reason=cooldown")
        return False

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    reentry_block = int(getattr(CFG, "REENTRY_BLOCK_MINUTES", 30))
    if last_exit and (now - last_exit) < timedelta(minutes=reentry_block):
        if not momentum_positive:
            append_log("INFO", "SKIP", f"{sym} reason=reentry_block")
            return False
        append_log("INFO", "SKIP", f"{sym} reentry_block bypassed reason=positive_momentum")

    if halt_for_day:
        append_log("INFO", "SKIP", f"{sym} reason=halt_for_day")
        return False

    if pause_until and now < pause_until:
        append_log("INFO", "SKIP", f"{sym} reason=pause_entries")
        return False

    if sym in _positions():
        append_log("INFO", "SKIP", f"{sym} reason=already_held")
        return False

    max_pos = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
    if _open_positions_count() >= max_pos:
        append_log("INFO", "SKIP", f"{sym} reason=max_positions")
        return False

    required_value = float(entry) * max(1, int(qty or 1))
    if required_value > wallet_avail:
        append_log("INFO", "SKIP", f"{sym} reason=insufficient_wallet need={required_value:.2f} avail={wallet_avail:.2f}")
        return False

    if not RISK.can_enter_trade(sym, float(entry), _positions(), wallet_net, int(qty), sector=_sector_for_symbol(sym)):
        _apply_skip_cooldown(sym, "risk_guard")
        return False

    return True




def _compute_symbol_momentum_pct(sym: str) -> float:
    try:
        token = token_for_symbol(sym)
        kite = get_kite()
        data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=2), pd.Timestamp.now(), "15minute")
        df = pd.DataFrame(data)
        if df.empty or "close" not in df.columns or len(df) < 2:
            return 0.0
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2])
        if prev <= 0:
            return 0.0
        return ((last - prev) / prev) * 100.0
    except Exception:
        return 0.0


def _active_trade_universe() -> list:
    dyn = [str(x).strip().upper() for x in (STATE.get("research_universe") or []) if str(x).strip()]
    excl = set(load_excluded())
    out, seen = [], set()
    for s in dyn:
        if s in excl or s in seen:
            continue
        seen.add(s)
        out.append(s)
    try:
        limit = int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20) or 20)
        if limit > 0:
            out = out[:limit]
    except Exception:
        pass
    return out


def _strategy_registry_map() -> dict:
    return {str(x.get("family") or "").strip().lower(): dict(x) for x in STRATEGY_REGISTRY}


def _recent_family_performance_score(family: str) -> float:
    fam = str(family or "").strip().lower()
    if not fam:
        return 50.0
    try:
        rows = SA._read_csv_rows(SA.TRADE_HISTORY_PATH)  # type: ignore[attr-defined]
    except Exception:
        rows = []
    if not rows:
        return 50.0
    hist = [r for r in rows if str(r.get("strategy_family") or "").strip().lower() == fam][-20:]
    if not hist:
        return 50.0
    pnl = [float(r.get("pnl_inr") or 0.0) for r in hist]
    wins = [x for x in pnl if x > 0]
    win_rate = (len(wins) / len(pnl)) if pnl else 0.0
    recent = sum(pnl[-8:]) if pnl else 0.0
    score = (50.0 * win_rate) + (50.0 if recent > 0 else (30.0 if recent == 0 else 10.0))
    return max(0.0, min(100.0, score))


def _estimate_family_opportunity_count(family: str, symbols: list[str], sample_size: int = 14) -> int:
    fam = str(family or "").strip().lower()
    syms = list(dict.fromkeys([(s or "").strip().upper() for s in symbols if (s or "").strip()]))[: max(4, sample_size)]
    if not syms:
        return 0
    count = 0
    for sym in syms:
        q = _quality_metrics(sym)
        if not q.get("ok"):
            continue
        price = float(q.get("price") or 0.0)
        sma = float(q.get("sma20") or 0.0)
        sma_prev = float(q.get("sma20_prev") or 0.0)
        vol = float(q.get("vol_score") or 0.0)
        if price <= 0 or sma <= 0:
            continue
        dist_pct = ((price - sma) / sma) * 100.0
        if fam in ("trend_long", "fallback_long"):
            if price > sma and vol >= 1.0:
                count += 1
        elif fam == "pullback_long":
            if price >= (sma * 0.998) and sma >= sma_prev and vol >= 0.9:
                count += 1
        elif fam == "mean_reversion":
            if abs(dist_pct) <= 0.8 and vol >= 0.8:
                count += 1
        elif fam == "outlier_long":
            if price > (sma * 1.003) and vol >= 1.2:
                count += 1
        elif fam in ("short_breakdown", "fallback_short"):
            if price < sma and vol >= 1.0:
                count += 1
        elif fam == "outlier_short":
            if price < (sma * 0.997) and vol >= 1.2:
                count += 1
    return int(count)


def score_strategy_family(family: str, market_context: dict, universe_context: dict, recent_stats: dict | None = None) -> tuple[int, dict]:
    fam = str(family or "").strip().lower()
    registry = _strategy_registry_map().get(fam, {})
    regime = str(market_context.get("regime") or "UNKNOWN").upper()
    trend_direction = str(market_context.get("trend_direction") or "UNKNOWN").upper()
    opening_mode = str(market_context.get("opening_mode") or "OPEN_CLEAN").upper()
    volatility = str(market_context.get("volatility_state") or ("HIGH" if regime == "VOLATILE" else "NORMAL")).upper()
    quality = float(market_context.get("quality_score") or 50.0)
    sector_bias = float(universe_context.get("sector_bias") or 0.0)
    opp_count = int(universe_context.get("opportunity_count") or 0)
    htf_bias = float(market_context.get("htf_bias") or 0.0)
    recent_perf = float((recent_stats or {}).get("recent_perf_score") or _recent_family_performance_score(fam))

    preferred_regimes = [str(x).upper() for x in list(registry.get("preferred_regimes") or [])]
    preferred_struct = [str(x).upper() for x in list(registry.get("preferred_market_structure") or [])]
    direction = str(registry.get("direction") or "either").lower()

    regime_fit = 30.0 if regime in preferred_regimes else (20.0 if regime == "UNKNOWN" else 8.0)
    directional_fit = 12.0
    if direction == "long":
        directional_fit = 20.0 if trend_direction == "UP" else (10.0 if trend_direction == "UNKNOWN" else 4.0)
    elif direction == "short":
        directional_fit = 20.0 if trend_direction == "DOWN" else (10.0 if trend_direction == "UNKNOWN" else 4.0)
    volatility_fit = 8.0
    if regime == "VOLATILE":
        volatility_fit = 10.0 if fam in ("outlier_long", "outlier_short", "fallback_short") else 4.0
    elif volatility == "LOW":
        volatility_fit = 10.0 if fam in ("trend_long", "pullback_long", "mean_reversion") else 6.0

    opportunity_fit = min(15.0, float(opp_count) * 3.0)
    sector_fit = 10.0 if ((sector_bias >= 0 and direction != "short") or (sector_bias < 0 and direction == "short")) else 5.0
    htf_fit = 10.0 if ((htf_bias >= 0 and direction != "short") or (htf_bias < 0 and direction == "short")) else 4.0
    perf_fit = max(0.0, min(5.0, recent_perf / 20.0))

    if opening_mode not in preferred_struct:
        regime_fit = max(2.0, regime_fit - 6.0)
    if quality < 40 and fam in ("trend_long", "pullback_long"):
        volatility_fit = max(1.0, volatility_fit - 3.0)
    if regime in ("WEAK", "TRENDING_DOWN") and direction == "short":
        directional_fit = min(20.0, directional_fit + 3.0)
    if regime == "TRENDING_UP" and direction == "long":
        directional_fit = min(20.0, directional_fit + 2.0)

    total = regime_fit + directional_fit + volatility_fit + opportunity_fit + sector_fit + htf_fit + perf_fit
    score = int(round(max(0.0, min(100.0, total))))
    detail = {
        "regime_fit": round(regime_fit, 2),
        "directional_fit": round(directional_fit, 2),
        "volatility_fit": round(volatility_fit, 2),
        "opportunity_fit": round(opportunity_fit, 2),
        "sector_fit": round(sector_fit, 2),
        "htf_fit": round(htf_fit, 2),
        "performance_fit": round(perf_fit, 2),
        "opportunity_count": opp_count,
    }
    return score, detail


def _refresh_active_strategy_families(reason: str, regime: str, trend_direction: str, active_universe: list, research_universe: list) -> list[str]:
    prev_active = list(STATE.get("active_strategy_families") or [])
    market_context = {
        "regime": str(regime or "UNKNOWN").upper(),
        "trend_direction": str(trend_direction or "UNKNOWN").upper(),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN").upper(),
        "quality_score": float(get_opening_confidence(STATE.get("opening_metrics") or {})[0] if STATE.get("opening_metrics") else 60.0),
        "volatility_state": "HIGH" if str(regime).upper() == "VOLATILE" else "NORMAL",
        "htf_bias": 1.0 if str(trend_direction).upper() == "UP" else (-1.0 if str(trend_direction).upper() == "DOWN" else 0.0),
    }
    sector_snapshot = _sector_strength_snapshot(active_universe[:10] if active_universe else research_universe[:10])
    sector_bias = (sum(sector_snapshot.values()) / len(sector_snapshot)) if sector_snapshot else 0.0
    source_symbols = active_universe or research_universe or []

    scored = []
    score_map = {}
    append_log("INFO", "ROUTE", f"[ROUTE] recomputing top3 strategies reason={reason}")
    for st in STRATEGY_REGISTRY:
        fam = str(st.get("family") or "").strip().lower()
        opp_n = _estimate_family_opportunity_count(fam, source_symbols)
        universe_context = {"sector_bias": sector_bias, "opportunity_count": opp_n}
        score, detail = score_strategy_family(fam, market_context, universe_context, recent_stats=None)
        append_log("INFO", "ROUTE", f"[ROUTE] strategy_score family={fam} score={score}")
        score_map[fam] = {"score": score, "detail": detail}
        scored.append((fam, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    min_active = int(getattr(CFG, "STRATEGY_MIN_ACTIVE_SCORE", 40) or 40)
    active = [fam for fam, sc in scored if sc >= min_active][:3]
    STATE["active_strategy_families"] = list(active)
    STATE["active_strategy_last_refresh"] = float(time.time())
    STATE["active_strategy_last_reason"] = str(reason or "timer")
    STATE["strategy_scores_last"] = score_map
    hist = list(STATE.get("strategy_selection_history") or [])
    hist.append(
        {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "reason": str(reason or "timer"),
            "regime": str(regime or "UNKNOWN"),
            "top3": list(active),
        }
    )
    STATE["strategy_selection_history"] = hist[-200:]
    append_log("INFO", "ROUTE", f"[ROUTE] active_top3={','.join(active) if active else 'none'}")
    if tuple(prev_active) != tuple(active):
        _append_runtime_event(
            "route_changes_today",
            {
                "reason": str(reason or "timer"),
                "from_top3": prev_active,
                "to_top3": list(active),
                "regime": str(regime or "UNKNOWN"),
                "trend_direction": str(trend_direction or "UNKNOWN"),
            },
            limit=240,
        )
        _record_research_event("top3_change", f"reason={reason}", from_top3=prev_active, to_top3=list(active))
    return active


def _maybe_refresh_active_strategy_families(regime: str, trend_direction: str, active_universe: list, research_universe: list) -> list[str]:
    last = float(STATE.get("active_strategy_last_refresh") or 0.0)
    refresh_min = int(getattr(CFG, "STRATEGY_SELECTION_REFRESH_MINUTES", 10) or 10)
    elapsed_min = ((time.time() - last) / 60.0) if last > 0 else 999.0
    prev_regime = str(STATE.get("strategy_last_regime") or "")
    cur_regime = str(regime or "UNKNOWN").upper()
    dry_n = int(STATE.get("top3_dry_cycles") or 0)
    dry_thr = int(getattr(CFG, "TOP3_DRY_CYCLE_THRESHOLD", 5) or 5)
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)

    reason = ""
    if not list(STATE.get("active_strategy_families") or []):
        reason = "init"
    elif prev_regime and prev_regime != cur_regime:
        reason = "regime_change"
    elif elapsed_min >= max(1, refresh_min):
        reason = "timer"
    elif bool(STATE.get("fallback_mode_active")):
        reason = "fallback_mode"
    elif dry_n >= dry_thr:
        reason = "top3_dry"
    elif no_entry_cycles >= max(3, dry_thr):
        reason = "no_entry_cycles"

    STATE["strategy_last_regime"] = cur_regime
    if reason:
        return _refresh_active_strategy_families(reason, cur_regime, trend_direction, active_universe, research_universe)
    return list(STATE.get("active_strategy_families") or [])


def _scan_family(family: str, universe: list, max_new: int, universe_source: str) -> int:
    fam = str(family or "").strip().lower()
    if max_new <= 0:
        return 0
    if fam == "trend_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "pullback_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_pullback_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "mean_reversion":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_mean_reversion_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "fallback_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_mean_reversion_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "outlier_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_vwap_ema_signal, strategy_family=fam, universe_source=universe_source)
    if fam in ("short_breakdown", "fallback_short", "outlier_short"):
        if not bool(getattr(CFG, "ENABLE_SHORT_MODE", True)):
            return 0
        return _scan_short_entries(universe, max_new=max_new, strategy_family=fam, universe_source=universe_source)
    return 0


def _scan_top3_families(universe: list, families: list[str], max_new: int, universe_source: str) -> int:
    if not universe or max_new <= 0:
        return 0
    fams = [str(f).strip().lower() for f in families if str(f).strip()]
    prev_source = str(STATE.get("last_route_universe_source") or "n/a")
    STATE["last_route_universe_source"] = str(universe_source or "n/a")
    if prev_source != STATE["last_route_universe_source"]:
        _append_runtime_event(
            "route_changes_today",
            {"reason": "universe_source_change", "from_source": prev_source, "to_source": STATE["last_route_universe_source"], "families": list(fams)},
            limit=240,
        )
        _record_research_event("route_change", f"source={prev_source}->{STATE['last_route_universe_source']}", active_top3=list(fams))
        _record_universe_change(
            "route_driven_universe_change",
            STATE["last_route_universe_source"],
            [],
            [],
            fallback_active=bool(STATE.get("fallback_mode_active")),
        )
    append_log("INFO", "ROUTE", f"[ROUTE] scanning families={','.join(fams) if fams else 'none'} source={universe_source}")
    opened = 0
    for fam in fams:
        if opened >= max_new:
            break
        opened += _scan_family(fam, universe, max_new=max_new - opened, universe_source=universe_source)
    return opened

def _load_research_universe_from_file() -> list:
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = set(load_excluded())
    syms = [s for s in syms if s not in excl]
    if syms:
        STATE["research_universe"] = list(syms)
        append_log("INFO", "UNIV", f"Loaded from file size={len(syms)}")
    return syms


def _resolve_trade_universe() -> list:
    if isinstance(getattr(RE, "research_state", {}).get("last_report"), dict):
        STATE["research_last_report"] = dict(getattr(RE, "research_state", {}).get("last_report"))
    dyn = _active_trade_universe()
    if dyn:
        append_log("INFO", "UNIV", f"Research universe loaded size={len(dyn)}")
        _record_research_event("night_or_live_universe", f"source=runtime size={len(dyn)}")
        return dyn

    from_file = _load_research_universe_from_file()
    if from_file:
        append_log("INFO", "UNIV", f"Research universe loaded size={len(from_file)}")
        _record_research_event("night_or_live_universe", f"source=file size={len(from_file)}")
        return from_file

    fallback = load_universe_trading()
    if fallback:
        append_log("INFO", "UNIV", f"Fallback static size={len(fallback)}")
        _record_research_event("fallback_activation", f"source=trading_file size={len(fallback)}")
    return fallback


def _record_research_event(event_type: str, message: str, **extra):
    payload = {"event": event_type, "message": message}
    if extra:
        payload.update(extra)
    _append_runtime_event("research_events", payload, limit=400)
    append_log("INFO", "RESEARCH", f"[RESEARCH] {event_type} {message}")


def _record_universe_change(reason: str, source: str, added: list[str], removed: list[str], fallback_active: bool = False):
    rec = {
        "reason": reason,
        "source": source,
        "added": list(added or []),
        "removed": list(removed or []),
        "fallback_active": bool(fallback_active),
    }
    _append_runtime_event("universe_changes_today", rec, limit=240)
    append_log(
        "INFO",
        "UNIV",
        f"[UNIV_CHANGE] reason={reason} source={source} added={','.join(added or []) or '-'} removed={','.join(removed or []) or '-'} fallback={bool(fallback_active)}",
    )
    _record_research_event("universe_change", f"reason={reason} source={source}", added=added, removed=removed, fallback_active=bool(fallback_active))


def _sector_strength_snapshot(research_universe: list) -> dict:
    bucket = {}
    for sym in research_universe or []:
        sec = str(SECTOR_MAP.get(str(sym).upper(), "OTHER") or "OTHER").upper()
        mom = _compute_symbol_momentum_pct(str(sym).upper())
        bucket.setdefault(sec, []).append(float(mom))
    out = {}
    for sec, arr in bucket.items():
        out[sec] = (sum(arr) / len(arr)) if arr else 0.0
    return out


def _active_score_metrics(symbol: str, sector_strength: dict) -> dict:
    try:
        df = _htf_fetch(symbol, days=4)
        if df.empty or "close" not in df.columns or "volume" not in df.columns:
            return {"ok": False}
        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        if len(close) < 8:
            return {"ok": False}

        intraday_change = ((float(close.iloc[-1]) - float(close.iloc[-4])) / float(close.iloc[-4]) * 100.0) if float(close.iloc[-4]) > 0 else 0.0
        rel_vol = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0
        momentum = ((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100.0) if float(close.iloc[-2]) > 0 else 0.0
        vwap = (close * vol).sum() / max(1.0, float(vol.sum()))
        vwap_distance = ((float(close.iloc[-1]) - float(vwap)) / float(vwap) * 100.0) if float(vwap) > 0 else 0.0
        sec = str(SECTOR_MAP.get(symbol, "OTHER") or "OTHER").upper()
        sec_strength = float(sector_strength.get(sec, 0.0))

        score = (0.35 * intraday_change) + (0.25 * rel_vol) + (0.20 * momentum) + (0.10 * vwap_distance) + (0.10 * sec_strength)
        return {"ok": True, "score": float(score)}
    except Exception:
        return {"ok": False}


def build_active_universe(research_universe: list) -> list:
    base = [str(s).strip().upper() for s in (research_universe or []) if str(s).strip()]
    prev = list(STATE.get("active_universe") or [])
    if not base:
        STATE["active_universe"] = []
        if prev:
            _record_universe_change("active_universe_empty", "active_universe", [], prev, fallback_active=bool(STATE.get("fallback_mode_active")))
        return []
    n = int(getattr(CFG, "ACTIVE_UNIVERSE_SIZE", 8) or 8)
    sec_strength = _sector_strength_snapshot(base)
    scored = []
    for sym in base:
        m = _active_score_metrics(sym, sec_strength)
        if m.get("ok"):
            scored.append((sym, float(m.get("score") or 0.0)))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in scored[: max(1, n)]]
    if not out:
        out = base[: max(1, n)]
    STATE["active_universe"] = out
    STATE["active_universe_last_refresh"] = datetime.now(IST)
    append_log("INFO", "UNIV", f"Active universe refreshed size={len(out)}")
    prev_set, out_set = set(prev), set(out)
    added = sorted(list(out_set - prev_set))
    removed = sorted(list(prev_set - out_set))
    if added or removed:
        _record_universe_change("active_universe_refresh", "active_universe", added, removed, fallback_active=bool(STATE.get("fallback_mode_active")))
    return out


def refresh_active_universe_if_due(research_universe: list):
    mins = int(getattr(CFG, "ACTIVE_UNIVERSE_REFRESH_MINUTES", 10) or 10)
    last = STATE.get("active_universe_last_refresh")
    if not STATE.get("active_universe"):
        return build_active_universe(research_universe)
    if not isinstance(last, datetime):
        return build_active_universe(research_universe)
    if datetime.now(IST) - last >= timedelta(minutes=max(1, mins)):
        return build_active_universe(research_universe)
    return list(STATE.get("active_universe") or [])


def _passes_sector_entry_filter(sym: str) -> bool:
    """Explicit sector filter step before risk checks (separate from exposure guard)."""
    sec = str(SECTOR_MAP.get(sym, "OTHER") or "OTHER").upper()
    limit = int(getattr(CFG, "SECTOR_MAX_IN_UNIVERSE", 3) or 3)
    held = 0
    for hs, tr in _positions().items():
        hsec = str((tr or {}).get("sector") or SECTOR_MAP.get(str(hs).upper(), "OTHER") or "OTHER").upper()
        if hsec == sec:
            held += 1
    if held >= limit:
        append_log("INFO", "SECTOR", f"{sym} sector={sec} cap reached ({held}/{limit}) -> skip")
        return False
    return True



def get_research_rank(symbol: str, research_universe: list) -> int:
    sym = (symbol or "").strip().upper()
    if not sym:
        return -1
    for idx, s in enumerate(research_universe or [], start=1):
        if str(s).strip().upper() == sym:
            return idx
    return -1


def is_top_ranked_symbol(symbol: str, research_universe: list, top_n: int) -> bool:
    rank = get_research_rank(symbol, research_universe)
    return rank > 0 and rank <= max(1, int(top_n or 1))


def _research_score_for_symbol(symbol: str):
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    reports = []
    if isinstance(STATE.get("research_last_report"), dict):
        reports.append(STATE.get("research_last_report"))
    if isinstance(getattr(RE, "research_state", {}).get("last_report"), dict):
        reports.append(getattr(RE, "research_state", {}).get("last_report"))

    for report in reports:
        for r in list(report.get("top_ranked") or []):
            if str((r or {}).get("symbol") or "").strip().upper() == sym:
                try:
                    return float(r.get("final_score"))
                except Exception:
                    continue
    return None


def _weak_market_quality_metrics(symbol: str) -> dict:
    try:
        token = token_for_symbol(symbol)
        kite = get_kite()
        data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=45), pd.Timestamp.now(), "day")
        df = pd.DataFrame(data)
        if df.empty or "close" not in df.columns or "volume" not in df.columns or len(df) < 22:
            return {"ok": False, "reason": "weak_momentum"}

        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        sma20 = close.rolling(20).mean()
        if len(sma20.dropna()) < 2:
            return {"ok": False, "reason": "weak_momentum"}

        price = float(close.iloc[-1])
        sma_curr = float(sma20.iloc[-1])
        sma_prev = float(sma20.iloc[-2])
        vol_score = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0

        price_gt_sma = price > sma_curr
        slope_pos = sma_curr > sma_prev
        vol_ok = vol_score > float(getattr(CFG, "WEAK_MARKET_MIN_VOLUME_SCORE", 1.0) or 1.0)

        if not (price_gt_sma and slope_pos and vol_ok):
            return {
                "ok": False,
                "reason": "weak_momentum",
                "price_gt_sma20": price_gt_sma,
                "sma20_slope_pos": slope_pos,
                "volume_score": vol_score,
            }

        return {
            "ok": True,
            "price_gt_sma20": price_gt_sma,
            "sma20_slope_pos": slope_pos,
            "volume_score": vol_score,
        }
    except Exception:
        return {"ok": False, "reason": "weak_momentum"}


def passes_weak_market_filter(symbol: str, research_universe: list) -> tuple[bool, str, dict]:
    sym = (symbol or "").strip().upper()
    top_n = int(getattr(CFG, "WEAK_MARKET_TOP_N", 10) or 10)
    if not is_top_ranked_symbol(sym, research_universe, top_n):
        return False, "not_top_ranked", {}

    score = _research_score_for_symbol(sym)
    min_score = float(getattr(CFG, "WEAK_MARKET_MIN_SCORE", 0.90) or 0.90)
    if score is None or score < min_score:
        return False, "score_too_low", {"score": score}

    q = _weak_market_quality_metrics(sym)
    if not q.get("ok"):
        return False, str(q.get("reason") or "weak_momentum"), q

    meta = {"rank": get_research_rank(sym, research_universe), "score": score}
    meta.update(q)
    return True, "allowed", meta


def is_market_entry_allowed(symbol: str, regime: str, research_universe: list) -> tuple[bool, str, dict]:
    rg = str(regime or "UNKNOWN").upper()
    if rg in ("WEAK", "TRENDING_DOWN"):
        return passes_weak_market_filter(symbol, research_universe)
    return True, "allowed", {}



def _open_short_positions_count() -> int:
    c = 0
    for _s, tr in _positions().items():
        if str((tr or {}).get("side") or "LONG").upper() == "SHORT":
            c += 1
    return c


def _calc_pnl(entry: float, ltp: float, qty: int, side: str = "LONG") -> tuple[float, float]:
    side_u = str(side or "LONG").upper()
    if side_u == "SHORT":
        pnl_inr = (entry - ltp) * qty
        pnl_pct = ((entry - ltp) / entry * 100.0) if entry > 0 else 0.0
    else:
        pnl_inr = (ltp - entry) * qty
        pnl_pct = ((ltp - entry) / entry * 100.0) if entry > 0 else 0.0
    return float(pnl_inr), float(pnl_pct)


def _htf_fetch(symbol: str, days: int = 20) -> pd.DataFrame:
    try:
        token = token_for_symbol(symbol)
        data = get_kite().historical_data(
            token,
            pd.Timestamp.now() - pd.Timedelta(days=days),
            pd.Timestamp.now(),
            str(getattr(CFG, "HTF_INTERVAL", "15m") or "15m"),
        )
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    c = close.astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return (100.0 - (100.0 / (1.0 + rs))).astype(float).fillna(50.0)


def _nifty_reference_return(days: int = 45, bars: int = 20) -> float | None:
    ref_sym = str(getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES") or "NIFTYBEES").strip().upper()
    ref_df = _htf_fetch(ref_sym, days=days)
    if ref_df.empty or "close" not in ref_df.columns:
        return None
    ref_close = ref_df["close"].astype(float)
    if len(ref_close) <= bars:
        return None
    base = float(ref_close.iloc[-(bars + 1)])
    last = float(ref_close.iloc[-1])
    if base <= 0:
        return None
    return ((last - base) / base) * 100.0


def _session_bucket(now: datetime | None = None) -> str:
    t = (now or datetime.now(IST)).time()
    if t >= datetime.strptime("09:15", "%H:%M").time() and t < datetime.strptime("09:30", "%H:%M").time():
        return "EARLY"
    if t >= datetime.strptime("09:30", "%H:%M").time() and t < datetime.strptime("13:30", "%H:%M").time():
        return "MAIN"
    if t >= datetime.strptime("13:30", "%H:%M").time() and t <= datetime.strptime("15:00", "%H:%M").time():
        return "LATE"
    return "OFF"


def _in_open_filter_window(now: datetime | None = None) -> bool:
    t = (now or datetime.now(IST)).time()
    sh, sm = _parse_hhmm(getattr(CFG, "OPEN_FILTER_START", "09:15"))
    eh, em = _parse_hhmm(getattr(CFG, "OPEN_FILTER_END", "09:30"))
    start = datetime.now(IST).replace(hour=sh, minute=sm, second=0, microsecond=0).time()
    end = datetime.now(IST).replace(hour=eh, minute=em, second=0, microsecond=0).time()
    return start <= t <= end


def _compute_opening_metrics() -> dict:
    out = {
        "gap_pct": 0.0,
        "first_5m_range_pct": 0.0,
        "direction_clear": False,
        "spread_quality": "UNKNOWN",
        "volume_quality": "UNKNOWN",
        "valid": False,
        "data_state": "INCOMPLETE",
        "feed_error": False,
    }
    try:
        d1 = yf.download("^NSEI", period="7d", interval="1d", auto_adjust=False, progress=False, threads=False)
        m5 = yf.download("^NSEI", period="2d", interval="5m", auto_adjust=False, progress=False, threads=False)
        if d1 is None or d1.empty or m5 is None or m5.empty:
            return out
        d1 = d1.dropna()
        m5 = m5.dropna()
        if len(d1) < 2 or len(m5) < 3:
            return out

        prev_close = float(d1["Close"].astype(float).iloc[-2])
        open_today = float(d1["Open"].astype(float).iloc[-1])
        gap_pct = ((open_today - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

        # first intraday candle of current day
        idx = m5.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC").tz_convert(IST)
        else:
            idx = idx.tz_convert(IST)
        m5 = m5.copy()
        m5.index = idx
        today = datetime.now(IST).date()
        day_df = m5[m5.index.date == today]
        if day_df.empty:
            return out
        first = day_df.iloc[0]
        o5 = float(first.get("Open", 0.0) or 0.0)
        h5 = float(first.get("High", 0.0) or 0.0)
        l5 = float(first.get("Low", 0.0) or 0.0)
        c_last = float(day_df["Close"].astype(float).iloc[-1])
        ma3 = float(day_df["Close"].astype(float).rolling(3).mean().dropna().iloc[-1]) if len(day_df) >= 3 else c_last
        first_5m_range_pct = ((h5 - l5) / o5 * 100.0) if o5 > 0 else 0.0
        direction_clear = abs((c_last - o5) / o5 * 100.0) >= 0.15 and ((c_last > ma3 and c_last > o5) or (c_last < ma3 and c_last < o5))

        vol = day_df["Volume"].astype(float) if "Volume" in day_df.columns else pd.Series(dtype=float)
        volume_quality = "GOOD"
        if not vol.empty and len(vol) > 1:
            v0 = float(vol.iloc[0])
            vm = float(vol.tail(min(20, len(vol))).mean())
            volume_quality = "GOOD" if vm <= 0 or (v0 / vm) >= 0.8 else "LOW"

        max_range = float(getattr(CFG, "MAX_SAFE_FIRST_5M_RANGE_PCT", 1.2) or 1.2)
        spread_quality = "GOOD" if first_5m_range_pct <= max_range * 1.4 else "WIDE"

        out.update(
            {
                "gap_pct": float(gap_pct),
                "first_5m_range_pct": float(first_5m_range_pct),
                "direction_clear": bool(direction_clear),
                "spread_quality": spread_quality,
                "volume_quality": volume_quality,
                "valid": True,
                "data_state": "READY",
                "feed_error": False,
            }
        )
        return out
    except Exception:
        out["data_state"] = "FEED_ERROR"
        out["feed_error"] = True
        return out


def get_opening_confidence(metrics: dict | None = None) -> tuple[int, dict]:
    m = dict(metrics or _compute_opening_metrics() or {})
    gap = abs(float(m.get("gap_pct") or 0.0))
    rng = float(m.get("first_5m_range_pct") or 0.0)
    max_gap = float(getattr(CFG, "MAX_SAFE_GAP_PCT", 0.8) or 0.8)
    max_rng = float(getattr(CFG, "MAX_SAFE_FIRST_5M_RANGE_PCT", 1.2) or 1.2)
    dir_ok = bool(m.get("direction_clear"))
    spread_q = str(m.get("spread_quality") or "UNKNOWN").upper()
    volume_q = str(m.get("volume_quality") or "UNKNOWN").upper()

    parts = []
    considered = []

    if max_gap > 0:
        gscore = max(0.0, min(100.0, 100.0 * (1.0 - (gap / (max_gap * 1.5)))))
        parts.append(gscore)
        considered.append("gap")

    if rng > 0 and max_rng > 0:
        rscore = max(0.0, min(100.0, 100.0 * (1.0 - (rng / (max_rng * 1.6)))))
        parts.append(rscore)
        considered.append("range")

    if bool(m.get("valid")):
        parts.append(100.0 if dir_ok else 30.0)
        considered.append("trend")

    if volume_q != "UNKNOWN":
        parts.append(100.0 if volume_q == "GOOD" else 25.0)
        considered.append("volume")

    if spread_q != "UNKNOWN":
        parts.append(100.0 if spread_q == "GOOD" else 20.0)
        considered.append("spread")

    ignored = []
    if volume_q == "UNKNOWN":
        ignored.append("volume")
    if spread_q == "UNKNOWN":
        ignored.append("spread")
    if rng <= 0:
        ignored.append("opening_range")

    score = int(round(sum(parts) / len(parts))) if parts else 55
    score = max(0, min(100, score))
    meta = {
        "considered": considered,
        "ignored": ignored,
        "data_state": str(m.get("data_state") or "INCOMPLETE"),
        "feed_error": bool(m.get("feed_error")),
    }
    return score, meta


def get_opening_mode() -> tuple[str, dict]:
    if not bool(getattr(CFG, "USE_ADAPTIVE_OPEN_FILTER", True)):
        return "OPEN_CLEAN", {"valid": False}
    if not _in_open_filter_window():
        return "OPEN_CLEAN", {"valid": False}

    m = _compute_opening_metrics()
    gap = abs(float(m.get("gap_pct") or 0.0))
    max_gap = float(getattr(CFG, "MAX_SAFE_GAP_PCT", 0.8) or 0.8)
    spread_q = str(m.get("spread_quality") or "UNKNOWN").upper()
    volume_q = str(m.get("volume_quality") or "UNKNOWN").upper()

    conf, conf_meta = get_opening_confidence(m)
    m["confidence"] = conf
    m["confidence_meta"] = conf_meta

    append_log("INFO", "OPEN", f"gap_pct={float(m.get('gap_pct') or 0.0):.2f}")
    append_log("INFO", "OPEN", f"first_5m_range_pct={float(m.get('first_5m_range_pct') or 0.0):.2f}")
    append_log("INFO", "OPEN", f"spread_quality={spread_q}")
    append_log("INFO", "OPEN", f"volume_quality={volume_q}")
    if conf_meta.get("ignored"):
        append_log("INFO", "OPEN", f"missing data ignored: {','.join(conf_meta.get('ignored') or [])}")

    # Missing/unknown opening metrics (volume/spread/opening_range) must remain
    # an incomplete-data state and never escalate to confirmed_broken_feed.
    STATE["open_feed_retry_count"] = 0

    if bool(m.get("feed_error")):
        m["reason"] = "confirmed_broken_feed"
        return "OPEN_HARD_BLOCK", m

    if gap > max_gap * 1.5:
        m["reason"] = "confirmed_extreme_gap"
        return "OPEN_HARD_BLOCK", m

    if not bool(m.get("valid")):
        m["reason"] = "incomplete_opening_data"
        return "OPEN_MODERATE", m

    if spread_q == "UNKNOWN" or volume_q == "UNKNOWN":
        m["reason"] = "incomplete_opening_data"
        return "OPEN_MODERATE", m

    if conf < 40:
        m["reason"] = "unstable_open"
        return "OPEN_UNSAFE", m
    if conf < 70:
        m["reason"] = "incomplete_opening_data"
        return "OPEN_MODERATE", m
    m["reason"] = "opening_conditions_clean"
    return "OPEN_CLEAN", m


def _htf_volume_surge_score(symbol: str) -> float:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return 0.0
    return float(q.get("vol_score") or 0.0)


def confirm_long_htf(symbol: str, regime: str | None = None) -> bool:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return True
    rg = str(regime or (get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN").upper()
    htf_status = _htf_alignment_status(symbol, "BUY", rg)
    htf_score = 2 if htf_status == "PASS" else (1 if htf_status == "PARTIAL" else (1 if htf_status == "INSUFFICIENT_HISTORY" else 0))

    if rg in ("TRENDING", "TRENDING_UP"):
        ok = htf_score >= 2
    elif rg == "SIDEWAYS":
        ok = htf_score >= 1
    elif rg == "VOLATILE":
        vol_req = float(getattr(CFG, "VOLATILE_HTF_MIN_VOL_SCORE", 1.2) or 1.2)
        vol_score = _htf_volume_surge_score(symbol)
        ok = (htf_score >= 2) and (vol_score >= vol_req)
        if not ok:
            append_log(
                "INFO",
                "CONFIRM",
                f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score} vol_score={vol_score:.2f} vol_req={vol_req:.2f}",
            )
            return False
    else:
        ok = htf_score >= 1

    if ok and bool(getattr(CFG, "HTF_CONFIRM_RSI", False)):
        df = _htf_fetch(symbol)
        if df.empty or "close" not in df.columns:
            ok = False
        else:
            close = df["close"].astype(float)
            rsi = _calc_rsi(close)
            min_rsi = float(getattr(CFG, "HTF_LONG_MIN_RSI", 52.0) or 52.0)
            rv = float(rsi.iloc[-1]) if len(rsi) > 0 and pd.notna(rsi.iloc[-1]) else 0.0
            ok = rv >= min_rsi
            if not ok:
                append_log(
                    "INFO",
                    "CONFIRM",
                    f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score} rsi={rv:.2f} rsi_min={min_rsi:.2f}",
                )
                return False

    if not ok:
        append_log(
            "INFO",
            "CONFIRM",
            f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score}",
        )
    return ok


def confirm_short_htf(symbol: str) -> bool:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return True
    df = _htf_fetch(symbol)
    if df.empty or "close" not in df.columns:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
        return False
    ma_n = int(getattr(CFG, "HTF_CONFIRM_MA", 20) or 20)
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean().dropna()
    if len(ma) < 2:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
        return False
    ok = float(close.iloc[-1]) < float(ma.iloc[-1]) and float(ma.iloc[-1]) < float(ma.iloc[-2])
    if ok and bool(getattr(CFG, "HTF_CONFIRM_RSI", False)):
        rsi = _calc_rsi(close)
        max_rsi = float(getattr(CFG, "HTF_SHORT_MAX_RSI", 48.0) or 48.0)
        ok = float(rsi.iloc[-1]) <= max_rsi
    if not ok:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
    return ok


def _quality_metrics(symbol: str) -> dict:
    df = _htf_fetch(symbol, days=45)
    if df.empty or "close" not in df.columns or "volume" not in df.columns or len(df) < 22:
        return {"ok": False}
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    sma20 = close.rolling(20).mean().dropna()
    if len(sma20) < 2:
        return {"ok": False}
    vol_score = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0
    ret_20d = ((float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100.0) if float(close.iloc[-21]) > 0 else 0.0
    nifty_20d = _nifty_reference_return(days=45, bars=20)
    rs_vs_nifty = (ret_20d - nifty_20d) if nifty_20d is not None else None
    return {
        "ok": True,
        "price": float(close.iloc[-1]),
        "sma20": float(sma20.iloc[-1]),
        "sma20_prev": float(sma20.iloc[-2]),
        "vol_score": vol_score,
        "ret_20d": ret_20d,
        "rs_vs_nifty": rs_vs_nifty,
    }


def _opening_symbol_quality_ok(symbol: str, side: str = "BUY") -> bool:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return False
    price = float(q.get("price") or 0.0)
    sma20 = float(q.get("sma20") or 0.0)
    vol_score = float(q.get("vol_score") or 0.0)
    side_u = str(side or "BUY").upper()
    if side_u == "SHORT":
        return price < sma20 and vol_score >= float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2)
    return price > sma20 and vol_score >= 1.0


def _opening_size_multiplier() -> float:
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if mode == "OPEN_HARD_BLOCK":
        return 0.0
    if mode == "OPEN_UNSAFE":
        return float(getattr(CFG, "OPEN_UNSAFE_SIZE_MULTIPLIER", 0.25) or 0.25)
    if mode == "OPEN_MODERATE":
        return float(getattr(CFG, "OPEN_MODERATE_SIZE_MULTIPLIER", 0.5) or 0.5)
    if mode == "OPEN_CLEAN":
        return float(getattr(CFG, "OPEN_CLEAN_SIZE_MULTIPLIER", 1.0) or 1.0)
    return 1.0

def _opening_selective_entry_allowed(symbol: str, side: str = "BUY") -> tuple[bool, str]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return False, "invalid_symbol"
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    research_universe = _active_trade_universe()
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)
    fallback_cycles = int(getattr(CFG, "OPEN_MIN_TRADE_AFTER_NO_EXEC_CYCLES", 8) or 8)

    if mode == "OPEN_HARD_BLOCK":
        return False, "hard_block_opening"

    if mode == "OPEN_UNSAFE":
        top_n = int(getattr(CFG, "OPEN_UNSAFE_TOP_N", 5) or 5)
        min_score = float(getattr(CFG, "OPEN_UNSAFE_MIN_SCORE", 0.90) or 0.90)
        if research_universe and not is_top_ranked_symbol(sym, research_universe, top_n):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        score = _research_score_for_symbol(sym)
        if score is not None and score < min_score:
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        if not _opening_symbol_quality_ok(sym, side=side):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        return True, "allowed"

    if mode == "OPEN_MODERATE":
        top_n = int(getattr(CFG, "OPEN_MODERATE_TOP_N", 10) or 10)
        if research_universe and not is_top_ranked_symbol(sym, research_universe, top_n):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        if not _opening_symbol_quality_ok(sym, side=side):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        return True, "allowed"

    return True, "allowed"


def _session_quality_score() -> float:
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if mode == "OPEN_HARD_BLOCK":
        return 0.2
    if mode == "OPEN_UNSAFE":
        return 0.3
    if mode == "OPEN_MODERATE":
        return 0.6
    return 1.0


def _htf_alignment_status(symbol: str, side: str, regime: str) -> str:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return "PASS"
    df = _htf_fetch(symbol)
    if df.empty or "close" not in df.columns:
        return "INSUFFICIENT_HISTORY"
    ma_n = int(getattr(CFG, "HTF_CONFIRM_MA", 20) or 20)
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean().dropna()
    if len(ma) < 2:
        return "INSUFFICIENT_HISTORY"

    px = float(close.iloc[-1])
    ma_now = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-2])
    bucket = _session_bucket()
    strictness = str(STATE.get("confirm_strictness") or "STRICT").upper()
    rg = str(regime or "UNKNOWN").upper()
    side_u = str(side or "BUY").upper()

    if side_u == "SHORT":
        partial = px < ma_now
        full = partial and ma_now < ma_prev
        if rg in ("WEAK", "TRENDING_DOWN"):
            return "PASS" if full else ("PARTIAL" if partial else "FAIL")
        if bucket == "EARLY" or strictness == "MODERATE":
            return "PARTIAL" if partial else "FAIL"
        return "PASS" if full else ("PARTIAL" if partial else "FAIL")

    partial = px > ma_now
    full = partial and ma_now > ma_prev
    if rg in ("TRENDING", "TRENDING_UP"):
        if strictness == "STRICT" and bucket != "EARLY":
            return "PASS" if full else ("PARTIAL" if partial else "FAIL")
        return "PARTIAL" if partial else "FAIL"
    if rg == "SIDEWAYS":
        return "PARTIAL" if partial else "FAIL"
    if rg == "VOLATILE":
        vol_req = float(getattr(CFG, "VOLATILE_HTF_MIN_VOL_SCORE", 1.2) or 1.2)
        q = _quality_metrics(symbol)
        vol_score = float(q.get("vol_score") or 0.0) if q.get("ok") else 0.0
        if full and vol_score >= vol_req:
            return "PASS"
        return "FAIL"
    if rg in ("WEAK", "TRENDING_DOWN"):
        q = _quality_metrics(symbol)
        exceptional = bool(q.get("ok")) and float(q.get("vol_score") or 0.0) >= 1.3 and float(q.get("price") or 0.0) > float(q.get("sma20") or 0.0)
        if full and exceptional:
            return "PASS"
        return "PARTIAL" if partial and exceptional else "FAIL"

    if bucket == "EARLY" or strictness == "MODERATE":
        return "PARTIAL" if partial else "FAIL"
    return "PASS" if full else ("PARTIAL" if partial else "FAIL")


def _entry_tier_from_score(score: int) -> str:
    if score >= int(getattr(CFG, "ENTRY_FULL_MIN_SCORE", 80) or 80):
        return "FULL"
    if score >= int(getattr(CFG, "ENTRY_REDUCED_MIN_SCORE", 60) or 60):
        return "REDUCED"
    if score >= int(getattr(CFG, "ENTRY_MICRO_MIN_SCORE", 45) or 45):
        return "MICRO"
    return "BLOCK"


def _entry_tier_multiplier(tier: str) -> float:
    t = str(tier or "BLOCK").upper()
    if t == "FULL":
        return float(getattr(CFG, "ENTRY_FULL_SIZE_MULTIPLIER", 1.0) or 1.0)
    if t == "REDUCED":
        return float(getattr(CFG, "ENTRY_REDUCED_SIZE_MULTIPLIER", 0.5) or 0.5)
    if t == "MICRO":
        return float(getattr(CFG, "ENTRY_MICRO_SIZE_MULTIPLIER", 0.25) or 0.25)
    return 0.0


def _build_entry_confidence(symbol: str, side: str, sig: dict, regime: str, research_universe: list) -> dict:
    w_ltf = int(getattr(CFG, "CONFIRM_WEIGHT_LTF", 30) or 30)
    w_htf = int(getattr(CFG, "CONFIRM_WEIGHT_HTF", 25) or 25)
    w_reg = int(getattr(CFG, "CONFIRM_WEIGHT_REGIME", 15) or 15)
    w_rank = int(getattr(CFG, "CONFIRM_WEIGHT_RANK", 15) or 15)
    w_sec = int(getattr(CFG, "CONFIRM_WEIGHT_SECTOR", 10) or 10)
    w_vol = int(getattr(CFG, "CONFIRM_WEIGHT_VOLUME", 5) or 5)

    comps = {}
    total = 0.0

    ltf_ok = bool(sig)
    comps["ltf"] = "ok" if ltf_ok else "fail"
    if ltf_ok:
        total += w_ltf

    htf_status = _htf_alignment_status(symbol, side, regime)
    comps["htf"] = htf_status.lower()
    if htf_status == "PASS":
        total += w_htf
    elif htf_status == "PARTIAL":
        total += (w_htf * 0.6)
    elif htf_status == "INSUFFICIENT_HISTORY":
        # Missing HTF history should be a moderate reduction, not a hard fail.
        if str(regime or "UNKNOWN").upper() == "SIDEWAYS":
            total += (w_htf * 0.5)
        else:
            total += (w_htf * 0.35)

    rg = str(regime or "UNKNOWN").upper()
    side_u = str(side or "BUY").upper()
    allowed, _reason, _meta = is_market_entry_allowed(symbol, rg, research_universe)
    comps["regime"] = "ok" if allowed else "weak"
    if rg in ("TRENDING", "TRENDING_UP"):
        if side_u == "BUY":
            total += w_reg if allowed else (w_reg * 0.7)
            comps["bias"] = "LONG_FIRST"
        else:
            total += (w_reg * 0.35) if allowed else (w_reg * 0.2)
            comps["bias"] = "SHORT_EXCEPTIONAL"
    elif rg in ("WEAK", "TRENDING_DOWN"):
        if side_u == "SHORT":
            total += w_reg if allowed else (w_reg * 0.7)
            comps["bias"] = "SHORT_FIRST"
        else:
            total += (w_reg * 0.35) if allowed else (w_reg * 0.2)
            comps["bias"] = "LONG_EXCEPTIONAL"
    elif rg == "SIDEWAYS":
        total += (w_reg * 0.75) if allowed else (w_reg * 0.45)
        comps["bias"] = "BALANCED"
    elif rg == "VOLATILE":
        total += (w_reg * 0.6) if allowed else (w_reg * 0.35)
        comps["bias"] = "RISK_REDUCED"
    else:
        total += (w_reg * 0.5) if allowed else (w_reg * 0.25)
        comps["bias"] = "UNKNOWN"

    rank = get_research_rank(symbol, research_universe)
    rscore = _research_score_for_symbol(symbol)
    rank_ok = (rank > 0 and rank <= 10) or (rscore is not None and float(rscore) >= 0.85)
    comps["rank"] = "strong" if rank_ok else "weak"
    if rank_ok:
        total += w_rank
    elif rank > 0 and rank <= 20:
        total += (w_rank * 0.5)

    sec_map = _sector_strength_snapshot(research_universe)
    sec = _sector_for_symbol(symbol)
    sec_strength = float(sec_map.get(sec, 0.0)) if isinstance(sec_map, dict) else 0.0
    comps["sector"] = "strong" if sec_strength >= 0 else "weak"
    if sec_strength >= 0.5:
        total += w_sec
    elif sec_strength >= 0:
        total += (w_sec * 0.6)
    elif sec_strength > -0.5:
        total += (w_sec * 0.3)

    q = _quality_metrics(symbol)
    vol_score = float(q.get("vol_score") or 0.0) if q.get("ok") else 0.0
    comps["volume"] = "ok" if vol_score >= 1.0 else "weak"
    if vol_score >= 1.0:
        total += w_vol
    elif vol_score >= 0.8:
        total += (w_vol * 0.5)

    total = total * _session_quality_score()
    if rg == "VOLATILE":
        total = total * 0.75
    comps["session"] = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()

    score = int(round(max(0.0, min(100.0, total))))
    tier = _entry_tier_from_score(score)

    hard_block_reason = ""
    om = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    om_reason = str((STATE.get("opening_metrics") or {}).get("reason") or "")
    if om == "OPEN_HARD_BLOCK" and om_reason in ("confirmed_broken_feed", "confirmed_extreme_gap"):
        hard_block_reason = om_reason

    if tier == "MICRO":
        micro_n = int(getattr(CFG, "ENTRY_MICRO_TOP_N", 5) or 5)
        if research_universe and not is_top_ranked_symbol(symbol, research_universe, micro_n):
            tier = "BLOCK"
            score = min(score, int(getattr(CFG, "ENTRY_MICRO_MIN_SCORE", 45) or 45) - 1)
            comps["micro_rank"] = "fail"

    if _is_micro_mode_active() and not hard_block_reason:
        max_trades = int(getattr(CFG, "MICRO_MODE_MAX_TRADES", 2) or 2)
        done = int(STATE.get("micro_mode_trade_count") or 0)
        if done < max_trades:
            min_score = int(getattr(CFG, "MICRO_MODE_MIN_SCORE", 35) or 35)
            micro_n = int(getattr(CFG, "ENTRY_MICRO_TOP_N", 5) or 5)
            top_ranked = (not research_universe) or is_top_ranked_symbol(symbol, research_universe, micro_n)
            if score >= min_score and top_ranked and tier == "BLOCK":
                tier = "MICRO"
                comps["micro_override"] = "active"

    size_mult = _entry_tier_multiplier(tier)
    if _is_micro_mode_active() and tier == "MICRO":
        size_mult = min(size_mult, float(getattr(CFG, "MICRO_MODE_SIZE_MULTIPLIER", 0.25) or 0.25))

    return {
        "score": score,
        "tier": tier,
        "size_mult": size_mult,
        "components": comps,
        "hard_block": hard_block_reason,
    }


def _prune_micro_mode_events(now_ts: float | None = None):
    now_ts = float(now_ts or time.time())
    lookback_min = int(getattr(CFG, "MICRO_MODE_LOOKBACK_MINUTES", 45) or 45)
    cutoff = now_ts - (max(1, lookback_min) * 60)
    sig = [float(x) for x in list(STATE.get("signal_event_ts") or []) if float(x) >= cutoff]
    ent = [float(x) for x in list(STATE.get("entry_event_ts") or []) if float(x) >= cutoff]
    STATE["signal_event_ts"] = sig
    STATE["entry_event_ts"] = ent
    STATE["signals_seen_window"] = len(sig)
    STATE["entries_executed_window"] = len(ent)


def _record_signal_seen():
    now_ts = time.time()
    events = list(STATE.get("signal_event_ts") or [])
    events.append(now_ts)
    STATE["signal_event_ts"] = events
    _prune_micro_mode_events(now_ts)


def _record_entry_executed():
    now_ts = time.time()
    events = list(STATE.get("entry_event_ts") or [])
    events.append(now_ts)
    STATE["entry_event_ts"] = events
    _prune_micro_mode_events(now_ts)
    if bool(STATE.get("micro_mode_active")):
        STATE["micro_mode_trade_count"] = int(STATE.get("micro_mode_trade_count") or 0) + 1


def _is_micro_mode_active() -> bool:
    return bool(STATE.get("micro_mode_active"))


def _deactivate_micro_mode(reason: str = ""):
    if not bool(STATE.get("micro_mode_active")):
        return
    STATE["micro_mode_active"] = False
    STATE["micro_mode_trade_count"] = 0
    STATE["micro_mode_regime"] = ""
    append_log("INFO", "HEALTH", f"MICRO MODE deactivated{(' reason=' + reason) if reason else ''}")


def _overfilter_health_check():
    _prune_micro_mode_events()
    sig_n = int(STATE.get("signals_seen_window") or 0)
    ent_n = int(STATE.get("entries_executed_window") or 0)
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)
    sig_thr = int(getattr(CFG, "OVERFILTER_SIGNAL_THRESHOLD", 8) or 8)
    cyc_thr = int(getattr(CFG, "OVERFILTER_NO_ENTRY_CYCLES", 6) or 6)
    micro_sig_thr = int(getattr(CFG, "MICRO_MODE_SIGNAL_THRESHOLD", 5) or 5)

    if sig_n >= sig_thr and ent_n == 0 and no_entry_cycles >= cyc_thr:
        if str(STATE.get("confirm_strictness") or "STRICT").upper() != "MODERATE":
            STATE["confirm_strictness"] = "MODERATE"
            append_log("WARN", "HEALTH", "Over-filtered mode detected → downgrading strict HTF to moderate")
            _notify("[HEALTH] Over-filtered: valid signals detected but no executions. Downgrading strict HTF to moderate.")

    if sig_n >= micro_sig_thr and ent_n == 0 and no_entry_cycles >= cyc_thr:
        if not bool(STATE.get("micro_mode_active")):
            STATE["micro_mode_active"] = True
            STATE["micro_mode_trade_count"] = 0
            STATE["micro_mode_regime"] = str(STATE.get("last_regime") or "UNKNOWN")
            append_log("WARN", "HEALTH", "over-filter detected -> activating MICRO MODE")
            _notify("[HEALTH] over-filter detected -> activating MICRO MODE")

    if ent_n > 0:
        STATE["confirm_strictness"] = "STRICT"
        STATE["signals_seen_window"] = 0
        STATE["entries_executed_window"] = 0
        if bool(STATE.get("micro_mode_active")):
            _deactivate_micro_mode("normal_trades_resumed")


def generate_short_signal(symbol: str, strategy_family: str = "short_breakdown"):
    sym = (symbol or "").strip().upper()
    q = _quality_metrics(sym)
    if not q.get("ok"):
        return None
    price = float(q["price"])
    sma20 = float(q["sma20"])
    sma20_prev = float(q["sma20_prev"])
    vol_score = float(q["vol_score"])
    rs_vs_nifty = q.get("rs_vs_nifty")
    max_rs_short = float(getattr(CFG, "SHORT_RS_MAX_VS_NIFTY", -0.2) or -0.2)
    base_rs_ok = (rs_vs_nifty is None) or (float(rs_vs_nifty) <= max_rs_short)
    fam = str(strategy_family or "short_breakdown").strip().lower()
    if fam == "outlier_short":
        outlier_rs = (rs_vs_nifty is None) or (float(rs_vs_nifty) <= (max_rs_short - 0.3))
        outlier_vol = vol_score > max(1.4, float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2))
        outlier_dist = price < (sma20 * 0.997)
        cond = price < sma20 and sma20 < sma20_prev and outlier_vol and outlier_rs and outlier_dist
    elif fam == "fallback_short":
        fallback_vol = vol_score > float(getattr(CFG, "FALLBACK_MIN_VOLUME_SCORE", 1.2) or 1.2)
        cond = price < sma20 and fallback_vol and base_rs_ok
    else:
        cond = price < sma20 and sma20 < sma20_prev and vol_score > float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2) and base_rs_ok
        fam = "short_breakdown"
    if not cond:
        return None
    append_log("INFO", "SIG", f"family={fam} symbol={sym} setup=short_signal")
    return {
        "symbol": sym,
        "entry": price,
        "side": "SHORT",
        "volume_score": vol_score,
        "rs_vs_nifty": rs_vs_nifty,
        "strategy_family": fam,
    }


def _fallback_candidate_score(symbol: str) -> float | None:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return None
    price = float(q.get("price") or 0.0)
    sma20 = float(q.get("sma20") or 0.0)
    sma20_prev = float(q.get("sma20_prev") or 0.0)
    if price <= 0 or sma20 <= 0:
        return None
    trend_component = 1.0 if (price > sma20 and sma20 >= sma20_prev) else 0.0
    dist_component = max(-3.0, min(3.0, ((price - sma20) / sma20) * 100.0)) / 3.0
    vol_component = max(0.0, min(2.0, float(q.get("vol_score") or 0.0))) / 2.0
    rs = q.get("rs_vs_nifty")
    rs_component = 0.0 if rs is None else max(-3.0, min(3.0, float(rs))) / 3.0
    return (0.40 * trend_component) + (0.25 * dist_component) + (0.20 * vol_component) + (0.15 * rs_component)


def build_fallback_universe() -> list:
    base = load_universe_live() or load_universe_trading() or []
    top_n = int(getattr(CFG, "FALLBACK_TOP_N", 10) or 10)
    scored = []
    for sym in list(dict.fromkeys([s for s in base if s])):
        score = _fallback_candidate_score(sym)
        if score is not None:
            scored.append((sym, float(score)))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in scored[: max(10, top_n)]]
    if not out:
        out = list(dict.fromkeys([s for s in base if s]))[: max(10, top_n)]
    STATE["fallback_universe"] = out
    append_log("INFO", "UNIV", f"fallback universe built size={len(out)} scored={len(scored)}")
    return out


def _maybe_enter_short_from_signal(sig):
    if not sig:
        return False
    _record_signal_seen()
    sym = str(sig.get("symbol") or "").strip().upper()
    entry = float(sig.get("entry") or 0.0)
    strategy_family = str(sig.get("strategy_family") or "short_breakdown")
    universe_source = str(sig.get("universe_source") or "primary")
    if not sym or entry <= 0:
        return False

    if _open_short_positions_count() >= int(getattr(CFG, "MAX_SHORT_POSITIONS", 2) or 2):
        append_log("WARN", "RISK", "max short positions reached")
        return False

    qty, bucket_qty, risk_qty = _calc_qty(sym, entry)
    mult = float(getattr(CFG, "SHORT_SIZE_MULTIPLIER", 0.5) or 0.5)
    qty = max(1, int(math.floor(qty * mult))) if qty > 0 else 0
    strategy_tag = "short_breakdown"
    if bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        strategy_tag = "mtf_confirmed_short"
    qty = _apply_strategy_allocation(qty, strategy_tag)

    research_universe = _active_trade_universe()
    regime = str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN")
    decision = _build_entry_confidence(sym, "SHORT", sig, regime, research_universe)
    append_log(
        "INFO",
        "CONFIRM",
        f"symbol={sym} family={strategy_family} score={decision['score']} tier={decision['tier']} "
        f"htf={decision['components'].get('htf')} regime={decision['components'].get('regime')} "
        f"rank={decision['components'].get('rank')}",
    )
    if decision.get("hard_block"):
        append_log("WARN", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason={decision['hard_block']}")
        return False
    if decision.get("tier") == "BLOCK":
        htf_comp = str(decision.get("components", {}).get("htf") or "").lower()
        if htf_comp == "fail":
            append_log("INFO", "CONFIRM", f"SHORT blocked {sym} reason=HTF_Score_Below_Req_for_{regime} htf_status=FAIL htf_score=0")
        append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
        return False
    tier_mult = float(decision.get("size_mult") or 0.0)
    qty = max(1, int(math.floor(qty * tier_mult))) if qty > 0 and tier_mult > 0 else 0
    append_log("INFO", "CONFIRM", f"symbol={sym} tier={decision['tier']} reduced_size_applied mult={tier_mult:.2f}")

    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if _in_open_filter_window() and mode in ("OPEN_HARD_BLOCK", "OPEN_UNSAFE", "OPEN_MODERATE", "OPEN_CLEAN"):
        allowed_open, open_reason = _opening_selective_entry_allowed(sym, side="SHORT")
        if not allowed_open:
            append_log("INFO", "OPEN", f"blocked {sym} reason={open_reason}")
            return False
        open_mult = _opening_size_multiplier()
        if open_reason == "fallback_min_trade":
            append_log("INFO", "OPEN", "fallback min-trade activated after prolonged no-exec cycles")
            qty = 1
        else:
            qty = max(1, int(math.floor(qty * open_mult))) if qty > 0 and open_mult > 0 else 0
        if mode == "OPEN_UNSAFE":
            append_log("INFO", "OPEN", f"mode=OPEN_UNSAFE → micro-size selective entry allowed mult={open_mult:.2f}")
        elif mode == "OPEN_MODERATE":
            append_log("INFO", "OPEN", f"mode=OPEN_MODERATE → reduced-size entry allowed mult={open_mult:.2f}")
        else:
            append_log("INFO", "OPEN", "mode=OPEN_CLEAN → normal early-session trading allowed")

    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
        try:
            SA.record_skipped_signal({"symbol": sym, "side": "SHORT", "reason": "qty_zero", "strategy_tag": strategy_tag, "signal_price": entry})
        except Exception:
            pass
        _apply_skip_cooldown(sym, "qty_zero")
        return False

    if not _can_open_new_trade(sym, entry, qty, momentum_positive=False):
        return False

    mode = "LIVE" if is_live_enabled() else "PAPER"
    oid = None
    booked_entry = entry
    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            booked_entry = now_price
        append_log("INFO", "ORDER", f"symbol={sym} side=SELL family={strategy_family} tier={decision.get('tier')} qty={qty} entry={booked_entry:.2f}")
        oid = _place_live_order(kite, sym, "SELL", qty)
        if not oid:
            append_log("INFO", "SKIP", f"{sym} reason=order_failed")
            return False
        append_log("INFO", "FILL", f"symbol={sym} side=SELL qty={qty} fill={booked_entry:.2f} order_id={oid}")

    PM.set(sym, {
        "symbol": sym,
        "side": "SHORT",
        "entry": booked_entry,
        "entry_price": booked_entry,
        "qty": qty,
        "quantity": qty,
        "peak": 0.0,
        "peak_pct": 0.0,
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
        "order_id": oid,
        "strategy_tag": strategy_tag,
        "strategy_family": strategy_family,
        "confidence_tier": str(decision.get("tier") or "BLOCK"),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
        "market_regime": str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN"),
        "universe_source": universe_source,
        "sector": _sector_for_symbol(sym),
        "entry_time": datetime.now(IST).isoformat(timespec="seconds"),
    })
    STATE["entry_tier_for_cooldown"] = str(decision.get("tier") or "").upper()
    _set_cooldown()
    _log_trade_event("ORDER", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("FILL", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("TRADE", {**dict(_positions().get(sym) or {}), "symbol": sym, "exit_reason": "-"})
    _append_runtime_event("recent_entries", {"symbol": sym, "side": "SHORT", "qty": qty, "entry": booked_entry, "family": strategy_family, "tier": str(decision.get("tier") or "n/a"), "ts": datetime.now(IST).isoformat(timespec="seconds")}, limit=40)
    append_log("INFO", "ENTRY", f"SHORT {sym} family={strategy_family} tier={decision.get('tier')} source={universe_source} qty={qty} entry={booked_entry:.2f}")
    append_log("INFO", "EXEC", f"symbol={sym} side=SHORT size={int(round(tier_mult * 100))}%")
    if _is_micro_mode_active() and decision.get("tier") == "MICRO":
        append_log("INFO", "MICRO", f"executing symbol={sym} score={decision['score']} size={int(round(tier_mult * 100))}%")
    _notify(f"🟠 SHORT {mode}\nSymbol: {sym}\nQuantity: {qty}\nEntry: {booked_entry:.2f}")
    _record_entry_executed()
    return True

def _maybe_enter_from_signal(sig):
    if not sig:
        return False
    _record_signal_seen()
    sym = sig["symbol"].strip().upper()
    strategy_family = str(sig.get("strategy_family") or "trend_long")
    universe_source = str(sig.get("universe_source") or "primary")
    append_log("INFO", "SCAN", f"Scanning {sym}")

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    # 1) Market regime check (requested before buy gating)
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    research_universe = _active_trade_universe()
    allowed, reason, meta = is_market_entry_allowed(sym, regime, research_universe)
    weak_score = float(sig.get("signal_score") or 0.0)
    weak_score_min = float(getattr(CFG, "WEAK_MARKET_MIN_SCORE", 0.75) or 0.75)
    weak_size_mult = float(getattr(CFG, "WEAK_MARKET_SIZE_MULTIPLIER", 0.5) or 0.5)
    weak_long_allowed = True

    if regime == "WEAK":
        if weak_score >= weak_score_min:
            append_log("INFO", "MARKET", f"[MARKET] WEAK regime long allowed sym={sym} score={weak_score:.2f} size_mult={weak_size_mult:.1f}")
        else:
            append_log("INFO", "SKIP", f"[SKIP] {sym} reason=weak_regime_score_too_low score={weak_score:.2f} threshold={weak_score_min:.2f}")
            weak_long_allowed = False

    if regime in ("WEAK", "TRENDING_DOWN"):
        if not allowed:
            append_log("INFO", "MARKET", f"regime={regime} → long kept exceptional for {sym} reason={reason}")
            weak_cd = int(getattr(CFG, "MARKET_WEAK_COOLDOWN_MIN", 3) or 3)
            weak_cd = max(2, min(5, weak_cd))
            _apply_skip_cooldown(sym, "market_weak", minutes=weak_cd)
        else:
            append_log("INFO", "MARKET", f"regime={regime} → selective entry allowed for {sym} rank={meta.get('rank')} score={float(meta.get('score') or 0.0):.2f}")
    if regime == "WEAK" and not weak_long_allowed:
        return False
    elif regime == "UNKNOWN" and bool(getattr(CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", False)):
        append_log("WARN", "MARKET", f"regime=UNKNOWN → blocked {sym} reason=unknown_regime")
        return False

    # 2) Universe membership check against active dynamic universe (if available)
    active_dyn = _active_trade_universe()
    if active_dyn and sym not in set(active_dyn):
        append_log("INFO", "UNIV", f"{sym} not in research universe -> skip")
        return False

    # 3) Sector filter / cap
    if not _passes_sector_entry_filter(sym):
        return False

    entry = float(sig.get("entry") or 0.0)
    if entry <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=invalid_entry")
        return False

    momentum_pct = float(sig.get("momentum_pct") or _compute_symbol_momentum_pct(sym) or 0.0)
    momentum_threshold = float(getattr(CFG, "REENTRY_MOMENTUM_MIN_PCT", 0.0))
    momentum_positive = momentum_pct > momentum_threshold

    qty, bucket_qty, risk_qty = _calc_qty(sym, entry)
    strategy_tag = "primary_long"
    if regime in ("WEAK", "TRENDING_DOWN"):
        mult = weak_size_mult
        if mult > 0:
            reduced = max(1, int(math.floor(qty * mult)))
            if reduced < qty:
                qty = reduced
                append_log("INFO", "MARKET", f"regime={regime} → size reduced multiplier={mult}")
        strategy_tag = "weak_market_long"
    if bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)) and strategy_tag in ("primary_long", "weak_market_long"):
        strategy_tag = "mtf_confirmed_long"
    qty = _apply_strategy_allocation(qty, strategy_tag)

    decision = _build_entry_confidence(sym, "BUY", sig, regime, research_universe)
    append_log(
        "INFO",
        "CONFIRM",
        f"symbol={sym} family={strategy_family} score={decision['score']} tier={decision['tier']} "
        f"htf={decision['components'].get('htf')} regime={decision['components'].get('regime')} "
        f"rank={decision['components'].get('rank')}",
    )
    if decision.get("hard_block"):
        append_log("WARN", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason={decision['hard_block']}")
        return False
    if decision.get("tier") == "BLOCK":
        htf_comp = str(decision.get("components", {}).get("htf") or "").lower()
        if htf_comp == "fail":
            dscore = float(decision.get("score") or 0.0)
            if regime == "WEAK" and 30.0 <= dscore <= 60.0:
                tier_mult = max(0.0, weak_size_mult)
                append_log("INFO", "CONFIRM", f"BUY soft-allowed {sym} reason=HTF_Score_Below_Req_for_WEAK score={dscore:.0f} size_mult={tier_mult:.2f}")
            else:
                append_log("INFO", "CONFIRM", f"BUY blocked {sym} reason=HTF_Score_Below_Req_for_{regime} htf_status=FAIL htf_score=0")
                append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
                return False
        elif regime == "SIDEWAYS" and htf_comp in ("partial", "insufficient_history"):
            tier_mult = max(0.0, _entry_tier_multiplier("MICRO"))
            decision["tier"] = "MICRO"
            append_log("INFO", "CONFIRM", f"BUY soft-allowed {sym} reason=HTF_{htf_comp.upper()}_SIDEWAYS tier=MICRO")
        else:
            append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
            return False
    else:
        tier_mult = float(decision.get("size_mult") or 0.0)
    qty = max(1, int(math.floor(qty * tier_mult))) if qty > 0 and tier_mult > 0 else 0
    append_log("INFO", "CONFIRM", f"symbol={sym} tier={decision['tier']} reduced_size_applied mult={tier_mult:.2f}")

    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if _in_open_filter_window() and mode in ("OPEN_HARD_BLOCK", "OPEN_UNSAFE", "OPEN_MODERATE", "OPEN_CLEAN"):
        allowed_open, open_reason = _opening_selective_entry_allowed(sym, side="BUY")
        if not allowed_open:
            append_log("INFO", "OPEN", f"blocked {sym} reason={open_reason}")
            return False
        open_mult = _opening_size_multiplier()
        if open_reason == "fallback_min_trade":
            append_log("INFO", "OPEN", "fallback min-trade activated after prolonged no-exec cycles")
            qty = 1
        else:
            qty = max(1, int(math.floor(qty * open_mult))) if qty > 0 and open_mult > 0 else 0
        if mode == "OPEN_UNSAFE":
            append_log("INFO", "OPEN", f"mode=OPEN_UNSAFE → micro-size selective entry allowed mult={open_mult:.2f}")
        elif mode == "OPEN_MODERATE":
            append_log("INFO", "OPEN", f"mode=OPEN_MODERATE → reduced-size entry allowed mult={open_mult:.2f}")
        else:
            append_log("INFO", "OPEN", "mode=OPEN_CLEAN → normal early-session trading allowed")

    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
        try:
            SA.record_skipped_signal({"symbol": sym, "side": "BUY", "reason": "qty_zero", "strategy_tag": strategy_tag, "signal_price": entry, "market_regime": regime})
        except Exception:
            pass
        _apply_skip_cooldown(sym, "qty_zero")
        return False

    if not _can_open_new_trade(sym, entry, qty, momentum_positive=momentum_positive):
        return False

    mode = "LIVE" if is_live_enabled() else "PAPER"
    oid = None
    booked_entry = entry
    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            if now_price > entry * (1.0 + max_slip):
                append_log("INFO", "SKIP", f"{sym} reason=slippage")
                return False
            booked_entry = now_price
        append_log("INFO", "ORDER", f"symbol={sym} side=BUY family={strategy_family} tier={decision.get('tier')} qty={qty} entry={booked_entry:.2f}")
        oid = _place_live_order(kite, sym, "BUY", qty)
        if not oid:
            append_log("INFO", "SKIP", f"{sym} reason=order_failed")
            return False
        append_log("INFO", "FILL", f"symbol={sym} side=BUY qty={qty} fill={booked_entry:.2f} order_id={oid}")

    PM.set(sym, {
        "symbol": sym,
        "side": "BUY",
        "entry": booked_entry,
        "entry_price": booked_entry,
        "qty": qty,
        "quantity": qty,
        "peak": 0.0,
        "peak_pct": 0.0,
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
        "order_id": oid,
        "strategy_tag": strategy_tag,
        "strategy_family": strategy_family,
        "confidence_tier": str(decision.get("tier") or "BLOCK"),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
        "market_regime": regime,
        "universe_source": universe_source,
        "sector": _sector_for_symbol(sym),
        "entry_time": datetime.now(IST).isoformat(timespec="seconds"),
    })
    STATE["entry_tier_for_cooldown"] = str(decision.get("tier") or "").upper()
    _set_cooldown()
    _log_trade_event("ORDER", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("FILL", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("TRADE", {**dict(_positions().get(sym) or {}), "symbol": sym, "exit_reason": "-"})
    _append_runtime_event("recent_entries", {"symbol": sym, "side": "BUY", "qty": qty, "entry": booked_entry, "family": strategy_family, "tier": str(decision.get("tier") or "n/a"), "ts": datetime.now(IST).isoformat(timespec="seconds")}, limit=40)
    append_log("INFO", "SIG", f"BUY trigger {sym}")
    append_log("INFO", "ENTRY", f"BUY {sym} family={strategy_family} tier={decision.get('tier')} source={universe_source} qty={qty} mode={mode}")
    append_log("INFO", "EXEC", f"symbol={sym} side=BUY size={int(round(tier_mult * 100))}%")
    if _is_micro_mode_active() and decision.get("tier") == "MICRO":
        append_log("INFO", "MICRO", f"executing symbol={sym} score={decision['score']} size={int(round(tier_mult * 100))}%")
    _notify(
        f"🟢 BUY {mode}\n"
        f"Symbol: {sym}\n"
        f"Quantity: {qty}\n"
        f"Entry: {booked_entry:.2f}\n"
        f"Wallet: {float(STATE.get('wallet_net_inr') or 0.0):.2f}"
    )
    _record_entry_executed()
    return True


def get_positions_text():
    pos = _positions()
    if not pos:
        return "📍 Positions\n\nNo open positions."

    rows = []
    for sym, tr in sorted(pos.items()):
        entry, qty = _trade_entry_qty(tr)
        peak_pct = float(tr.get("peak_pct") or tr.get("peak") or 0.0)
        rows.append(f"- {sym} qty={qty} entry={entry:.2f} peak%={peak_pct:.2f}")

    return "📍 Positions\n\n" + "\n".join(rows)


def _current_open_pnl_breakdown():
    """Returns tuple: (profit_inr, loss_inr_abs) for currently open positions."""
    profit_inr = 0.0
    loss_inr_abs = 0.0
    kite = None
    try:
        kite = get_kite()
    except Exception:
        kite = None

    for sym, tr in sorted(_positions().items()):
        entry, qty = _trade_entry_qty(tr)
        if entry <= 0:
            continue
        ltp = _ltp(kite, sym) if kite else entry
        if ltp is None:
            ltp = entry
        side = str((tr or {}).get("side") or "LONG").upper()
        pnl, _ = _calc_pnl(entry, ltp, qty, side=side)
        if pnl >= 0:
            profit_inr += pnl
        else:
            loss_inr_abs += abs(pnl)

    return float(profit_inr), float(loss_inr_abs)


def _refresh_runtime_pnl_fields():
    _ensure_day_key()
    realized = float(STATE.get("today_pnl") or 0.0)
    prof, loss_abs = _current_open_pnl_breakdown()
    unrealized = float(prof - loss_abs)
    total = realized + unrealized
    STATE["realized_today"] = realized
    STATE["unrealized_now"] = unrealized
    STATE["pnl_so_far"] = total
    append_log("INFO", "PNL", f"realized_today={realized:.2f} unrealized_now={unrealized:.2f} pnl_so_far={total:.2f}")
    return realized, unrealized, total


def get_status_text():
    _ensure_day_key()
    RISK.sync_wallet(STATE)
    _sync_wallet_and_caps(force=False)
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    rows = []
    for sym, p in sorted(_positions().items()):
        e, q = _trade_entry_qty(p)
        rows.append(
            f"- {sym} {str(p.get('side') or 'BUY').upper()} qty={q} entry={e:.2f} "
            f"strategy={p.get('strategy_tag','-')} family={p.get('strategy_family','-')} "
            f"tier={p.get('confidence_tier','-')} source={p.get('universe_source','-')} regime={p.get('market_regime','-')}"
        )
    realized, unrealized, pnl_so_far = _refresh_runtime_pnl_fields()
    active_top3 = ",".join(list(STATE.get("active_strategy_families") or [])) or "none"
    selector_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    return (
        "📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(load_universe_trading())} symbols\n"
        f"Universe(live): {len(load_universe_live())} symbols\n"
        f"Open Positions: {_open_positions_count()}\n"
        f"Last Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Active Top3 Families: {active_top3}\n"
        f"Top3 Refresh Reason: {selector_reason}\n"
        f"Opening Mode: {STATE.get('opening_mode','OPEN_CLEAN')}\n"
        f"Realized Today: ₹{realized:.2f}\n"
        f"Unrealized Now: ₹{unrealized:.2f}\n"
        f"P/L So Far: ₹{pnl_so_far:.2f}\n\n"
        "Wallet/Caps:\n"
        f"- Wallet Net: ₹{float(STATE.get('wallet_net_inr') or 0):.2f}\n"
        f"- Wallet Available: ₹{float(STATE.get('wallet_available_inr') or 0):.2f}\n"
        f"- Exposure: ₹{_current_exposure_inr():.2f} / ₹{_max_exposure_inr():.2f} ({RUNTIME.get('MAX_EXPOSURE_PCT')}%)\n"
        f"- Daily Loss Cap (hard): ₹{float(STATE.get('daily_loss_cap_inr') or 0):.2f}\n"
        f"- Profit Milestone (soft): ₹{float(STATE.get('daily_profit_milestone_inr') or 0):.2f}\n\n"
        "Open Trades:\n"
        + ("\n".join(rows) if rows else "(none)")
        + "\n"
    )


def get_pnl_so_far_text() -> str:
    realized, unrealized, total = _refresh_runtime_pnl_fields()
    wallet = float(STATE.get("wallet_net_inr") or STATE.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    pct = (total / wallet * 100.0) if wallet > 0 else 0.0
    icon = "🟢" if total >= 0 else "🔴"
    return (
        "💰 P/L So Far\n\n"
        f"Realized Today: ₹{realized:.2f}\n"
        f"Unrealized Now: ₹{unrealized:.2f}\n"
        f"{icon} Total (So Far): ₹{total:.2f} ({pct:+.2f}%)"
    )


def get_research_text(limit: int = 16) -> str:
    events = list(STATE.get("research_events") or [])[-max(1, int(limit)) :]
    rep = dict(STATE.get("research_last_report") or {})
    lines = ["🔬 Research", ""]
    if rep:
        lines.append(
            f"Night Research: generated={rep.get('generated_at','-')} selected={int(rep.get('selected_count') or 0)}"
        )
        lines.append(f"Top Symbols: {','.join(list(rep.get('top_symbols') or [])[:8]) or 'n/a'}")
        lines.append("")
    if not events:
        lines.append("No runtime research events yet.")
        return "\n".join(lines)
    for e in events:
        ts = str(e.get("ts") or "-")
        et = str(e.get("event") or "event")
        msg = str(e.get("message") or "")
        lines.append(f"- [{ts}] {et}: {msg}")
    return "\n".join(lines)


def get_universe_changes_text(limit: int = 14) -> str:
    rows = list(STATE.get("universe_changes_today") or [])[-max(1, int(limit)) :]
    if not rows:
        return "🌌 Universe Changes\n\nNo universe changes tracked yet today."
    lines = ["🌌 Universe Changes", ""]
    for r in rows:
        ts = str(r.get("ts") or "-")
        reason = str(r.get("reason") or "n/a")
        src = str(r.get("source") or "n/a")
        add = ",".join(list(r.get("added") or [])) or "-"
        rem = ",".join(list(r.get("removed") or [])) or "-"
        fb = bool(r.get("fallback_active"))
        lines.append(f"- [{ts}] reason={reason} src={src} add={add} remove={rem} fallback={fb}")
    return "\n".join(lines)


def get_analytics_text() -> str:
    realized, unrealized, total = _refresh_runtime_pnl_fields()
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    top3 = ",".join(list(STATE.get("active_strategy_families") or [])) or "none"
    active_uni = ",".join(list(STATE.get("active_universe") or [])[:12]) or "none"
    open_rows = []
    for sym, p in sorted(_positions().items()):
        e, q = _trade_entry_qty(p)
        open_rows.append(f"{sym}:{str(p.get('side') or 'BUY').upper()} qty={q} entry={e:.2f}")
    entries = list(STATE.get("recent_entries") or [])[-5:]
    exits = list(STATE.get("recent_exits") or [])[-5:]
    ent_txt = "; ".join([f"{e.get('symbol')} {e.get('side')} q={e.get('qty')} @ {float(e.get('entry') or 0.0):.2f}" for e in entries]) if entries else "none"
    ex_txt = "; ".join([f"{e.get('symbol')} {e.get('side')} q={e.get('qty')} exit={float(e.get('exit') or 0.0):.2f} reason={e.get('reason')}" for e in exits]) if exits else "none"
    univ_changes = len(list(STATE.get("universe_changes_today") or []))
    route_changes = len(list(STATE.get("route_changes_today") or []))
    return (
        "📈 Analytics\n\n"
        f"Regime/Bias: {regime_now} / {bias}\n"
        f"Top3: {top3}\n"
        f"Active Universe: {active_uni}\n"
        f"Open Trades: {len(open_rows)}\n"
        f"Realized/Unrealized/Total: ₹{realized:.2f} / ₹{unrealized:.2f} / ₹{total:.2f}\n"
        f"Recent Entries: {len(entries)} | Recent Exits: {len(exits)}\n"
        f"Universe Changes Today: {univ_changes}\n"
        f"Route Changes Today: {route_changes}\n"
        f"Recent Entry Detail: {ent_txt}\n"
        f"Recent Exit Detail: {ex_txt}\n\n"
        f"Open Trade Snapshot: {('; '.join(open_rows[:6]) if open_rows else 'none')}"
    )


def get_strategy_selector_text() -> str:
    fams = list(STATE.get("active_strategy_families") or [])
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    scores = dict(STATE.get("strategy_scores_last") or {})
    lines = ["🧭 Strategy Selector", ""]
    lines.append(f"Active Top3: {','.join(fams) if fams else 'none'}")
    lines.append(f"Last Refresh Reason: {last_reason}")
    if scores:
        lines.append("")
        lines.append("Latest Family Scores")
        ranked = sorted(
            [(fam, int((meta or {}).get('score') or 0)) for fam, meta in scores.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        for fam, sc in ranked[:8]:
            lines.append(f"{fam}: {sc}")
    return "\n".join(lines)


def get_top3_text() -> str:
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    fams = list(STATE.get("active_strategy_families") or [])
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    last_refresh = float(STATE.get("active_strategy_last_refresh") or 0.0)
    last_refresh_txt = datetime.fromtimestamp(last_refresh, tz=IST).strftime("%H:%M:%S") if last_refresh > 0 else "n/a"
    return (
        "📌 Top 3 Strategies\n\n"
        f"Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Top3: {','.join(fams) if fams else 'none'}\n"
        f"Last Refresh: {last_refresh_txt}\n"
        f"Reason: {last_reason}"
    )


def get_strategy_scores_text() -> str:
    scores = dict(STATE.get("strategy_scores_last") or {})
    top3 = set([str(x).strip().lower() for x in list(STATE.get("active_strategy_families") or [])])
    if not scores:
        return "🧠 Strategy Scores\n\nNo scores available yet."
    rows = sorted(
        [(str(fam), int((meta or {}).get("score") or 0)) for fam, meta in scores.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    lines = ["🧠 Strategy Scores", ""]
    for fam, sc in rows:
        mark = "⭐" if fam.lower() in top3 else "•"
        lines.append(f"{mark} {fam}: {sc}")
    return "\n".join(lines[:20])


def get_regime_text() -> str:
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    opening_mode = str(STATE.get("opening_mode") or "OPEN_CLEAN")
    trend_direction = str(STATE.get("last_trend_direction") or "UNKNOWN")
    return (
        "🌐 Regime Snapshot\n\n"
        f"Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Opening Mode: {opening_mode}\n"
        f"Trend Direction: {trend_direction}"
    )


def get_route_status_text() -> str:
    fams = list(STATE.get("active_strategy_families") or [])
    route_source = str(STATE.get("last_route_universe_source") or "n/a")
    fallback_active = bool(STATE.get("fallback_mode_active"))
    dry_cycles = int(STATE.get("top3_dry_cycles") or 0)
    micro_active = bool(STATE.get("micro_mode_active"))
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    return (
        "📊 Route Status\n\n"
        f"Active Top3: {','.join(fams) if fams else 'none'}\n"
        f"Universe Source: {route_source}\n"
        f"Fallback Active: {fallback_active}\n"
        f"Top3 Dry Cycles: {dry_cycles}\n"
        f"Micro Mode: {micro_active}\n"
        f"Last Recompute Reason: {last_reason}"
    )




def get_trailing_status_text():
    _ensure_day_key()
    rows = []
    for sym, t in sorted(_positions().items()):
        entry, qty = _trade_entry_qty(t)
        if entry <= 0:
            continue
        kite = None
        try:
            kite = get_kite()
        except Exception:
            kite = None
        ltp = _ltp(kite, sym) if kite else entry
        if ltp is None:
            ltp = entry

        value = entry * qty
        side = str((t or {}).get("side") or "LONG").upper()
        pnl_inr, _ = _calc_pnl(entry, ltp, qty, side=side)
        peak_pnl_inr = float(t.get("peak_pnl_inr") or 0.0)
        peak_pnl_inr = max(peak_pnl_inr, pnl_inr)

        min_activate_inr = float(getattr(CFG, "MIN_TRAIL_ACTIVATE_INR", 8.0))
        activate_pct_of_position = float(getattr(CFG, "TRAIL_ACTIVATE_PCT_OF_POSITION", 0.4))
        trail_lock_ratio = float(getattr(CFG, "TRAIL_LOCK_RATIO", 0.5))
        trail_buffer_inr = float(getattr(CFG, "TRAIL_BUFFER_INR", 1.0))

        activate_inr = max(min_activate_inr, value * activate_pct_of_position / 100.0)
        trigger_inr = (peak_pnl_inr * trail_lock_ratio) - trail_buffer_inr
        trail_active = bool(t.get("trailing_active", t.get("trail_active", False)))

        rows.append(
            f"- {sym} qty={qty} entry={entry:.2f} ltp={ltp:.2f} value={value:.2f} "
            f"pnl_inr={pnl_inr:.2f} peak_pnl_inr={peak_pnl_inr:.2f} "
            f"trail_active={trail_active} activate@₹{activate_inr:.2f} trigger<=₹{trigger_inr:.2f}"
        )

    if not rows:
        return "📉 Trailing Status\n\nNo open trades."

    return "📉 Trailing Status\n\n" + "\n".join(rows)




def _scan_short_entries(universe: list, max_new: int, strategy_family: str = "short_breakdown", universe_source: str = "primary") -> int:
    opened = 0
    held = set(_positions().keys())
    for sym in [s for s in universe if s not in held][: max_new * 2]:
        if opened >= max_new:
            break
        append_log("INFO", "SCAN", f"Scanning {sym}")
        sig = generate_short_signal(sym, strategy_family=strategy_family)
        if not sig:
            SA.record_skipped_signal(
                {
                    "symbol": sym,
                    "side": "SHORT",
                    "reason": "no_short_signal",
                    "strategy_tag": "short_breakdown",
                    "strategy_family": strategy_family,
                }
            )
            continue
        sig.setdefault("strategy_family", strategy_family or "short_breakdown")
        sig.setdefault("universe_source", universe_source or "primary")
        if _maybe_enter_short_from_signal(sig):
            opened += 1
    return opened


def _maybe_send_eod_report():
    try:
        now = datetime.now(IST)
        hhmm = str(getattr(CFG, "EOD_REPORT_TIME", "15:16") or "15:16")
        hh, mm = [int(x) for x in hhmm.split(":", 1)]
        if now.hour < hh or (now.hour == hh and now.minute < mm):
            return
        day = now.strftime("%Y-%m-%d")
        if str(STATE.get("eod_report_sent_date") or "") == day:
            return
        if _open_positions_count() > 0:
            return
        append_log("INFO", "EOD", "Sending Telegram report")
        _notify(SA.generate_eod_report_text(STATE))
        STATE["eod_report_sent_date"] = day
    except Exception as e:
        append_log("WARN", "EOD", f"report generation failed: {e}")


def reconcile_broker_positions():
    if not is_live_enabled():
        return
    try:
        kite = get_kite()
        net_positions = (kite.positions() or {}).get("net") or []
    except Exception as e:
        append_log("WARN", "RECON", f"broker position fetch failed: {e}")
        return
    local = _positions()
    now_ts = datetime.now(IST).isoformat(timespec="seconds")
    broker_map = {}
    for p in net_positions:
        sym = str((p or {}).get("tradingsymbol") or "").strip().upper()
        qty = int((p or {}).get("quantity") or 0)
        if not sym or qty == 0:
            continue
        avg = float((p or {}).get("average_price") or 0.0)
        side = "BUY" if qty > 0 else "SHORT"
        broker_map[sym] = {"qty": abs(qty), "avg": avg, "side": side}
    for sym, bp in broker_map.items():
        if sym in local:
            tr = dict(local.get(sym) or {})
            lqty = int(tr.get("qty") or tr.get("quantity") or 0)
            lside = str(tr.get("side") or "BUY").upper()
            if lqty != int(bp["qty"]) or lside != str(bp["side"]):
                PM.set(sym, {**tr, "qty": int(bp["qty"]), "quantity": int(bp["qty"]), "side": str(bp["side"])})
                append_log("INFO", "RECON", f"synced_broker_qty symbol={sym} local_qty={lqty} broker_qty={bp['qty']} local_side={lside} broker_side={bp['side']}")
            continue
        PM.set(sym, {
            "symbol": sym,
            "side": bp["side"],
            "entry": float(bp["avg"] or 0.0),
            "entry_price": float(bp["avg"] or 0.0),
            "qty": int(bp["qty"]),
            "quantity": int(bp["qty"]),
            "peak": 0.0,
            "peak_pct": 0.0,
            "peak_pnl_inr": 0.0,
            "trail_active": False,
            "trailing_active": False,
            "order_id": None,
            "strategy_tag": "reconciled_external",
            "strategy_family": "reconciled_external",
            "confidence_tier": "RECON",
            "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
            "market_regime": str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN"),
            "universe_source": "broker_reconciled",
            "sector": _sector_for_symbol(sym),
            "entry_time": now_ts,
        })
        append_log("INFO", "RECON", f"synced_broker_open symbol={sym} side={bp['side']} qty={bp['qty']} entry={bp['avg']:.2f}")
        _log_trade_event("FILL", {**dict(local.get(sym) or {}), "symbol": sym})
    for sym in list(local.keys()):
        if sym not in broker_map and sym in _positions():
            append_log("WARN", "RECON", f"local_open_missing_on_broker symbol={sym}")
            tr = dict(_positions().get(sym) or {})
            PM.remove(sym)
            _log_trade_event("CLOSE", {**tr, "symbol": sym, "exit_reason": "RECON_BROKER_FLAT", "exit_time": now_ts})


def _scan_long_entries(universe: list, max_new: int, signal_fn=generate_signal, strategy_family: str = "trend_long", universe_source: str = "primary") -> int:
    before = _open_positions_count()

    def _signal_with_family(cands):
        sig = signal_fn(cands)
        if not sig and signal_fn is generate_signal:
            sig = generate_vwap_ema_signal(cands)
        if not sig and signal_fn is generate_signal:
            sig = generate_mean_reversion_signal(cands)
        if sig and strategy_family and not sig.get("strategy_family"):
            sig["strategy_family"] = strategy_family
        if sig and universe_source and not sig.get("universe_source"):
            sig["universe_source"] = universe_source
        if sig:
            append_log(
                "INFO",
                "SIG",
                f"family={str(sig.get('strategy_family') or strategy_family)} symbol={str(sig.get('symbol') or '').strip().upper()} setup={str(sig.get('strategy_setup') or sig.get('strategy_tag') or 'n/a')}",
            )
        return sig

    ee_process_entries(
        universe,
        _positions(),
        signal_fn=_signal_with_family,
        try_enter_fn=_maybe_enter_from_signal,
        max_new=max_new,
    )
    return max(0, _open_positions_count() - before)

def tick():
    _ensure_day_key()
    RISK.sync_wallet(STATE)
    _sync_wallet_and_caps(force=False)
    reconcile_broker_positions()
    _refresh_runtime_pnl_fields()
    RISK.check_day_drawdown_guard(STATE)

    if _past_force_exit_time() and _positions():
        append_log("WARN", "TIME", "FORCE_EXIT triggered")
        positions_before = _open_positions_count()
        ee_force_exit_all(_positions(), _close_position, reason="TIME")
        STATE["paused"] = True
        day_pnl = float(STATE.get("today_pnl") or 0.0)
        pnl_label = "Profit" if day_pnl >= 0 else "Loss"
        _notify(
            "🧾 Trading Day Brief\n"
            f"- Positions Closed (TIME): {positions_before}\n"
            f"- Day {pnl_label}: ₹{abs(day_pnl):.2f}\n"
            f"- Net Day PnL: ₹{day_pnl:.2f}\n"
            f"- Open Positions Now: {_open_positions_count()}"
        )


    # even when paused, force-exit check above still runs
    if STATE.get("paused"):
        return

    if STATE.get("halt_for_day"):
        append_log("WARN", "RISK", "halt_for_day active. Pausing loop.")
        STATE["paused"] = True
        return

    if float(STATE.get("daily_loss_cap_inr") or 0.0) > 0 and STATE["today_pnl"] <= -abs(float(STATE["daily_loss_cap_inr"])):
        append_log("WARN", "CAP", "Daily loss cap hit. Pausing loop.")
        STATE["paused"] = True
        return

    prof_milestone = float(STATE.get("daily_profit_milestone_inr") or 0.0)
    if prof_milestone > 0 and STATE["today_pnl"] >= prof_milestone and not STATE.get("profit_milestone_hit"):
        STATE["profit_milestone_hit"] = True
        append_log("INFO", "CAP", f"Profit milestone hit at ₹{STATE['today_pnl']:.2f}")
        _notify(f"🎯 Profit milestone hit: ₹{STATE['today_pnl']:.2f}")
        if not bool(RUNTIME.get("SOFT_PROFIT_TARGET", True)):
            STATE["paused"] = True
            return

    if getattr(CFG, "AUTO_PROMOTE_ENABLED", False) and not _positions() and _in_any_promote_window() and _cooldown_ok():
        if _market_stable():
            promote_universe(reason="AUTO_STABLE")

    ee_monitor_positions(
        STATE,
        _positions(),
        get_ltp=lambda sym: _ltp(get_kite(), sym),
        close_position=_close_position,
        force_exit_check=_past_force_exit_time,
    )

    if not _within_entry_window():
        return

    research_universe = _resolve_trade_universe()
    if not research_universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return
    _record_research_event("night_or_live_universe", f"resolved_size={len(research_universe)}")

    active_universe = refresh_active_universe_if_due(research_universe)
    if not active_universe:
        active_universe = list(research_universe[: int(getattr(CFG, "ACTIVE_UNIVERSE_SIZE", 8) or 8)])
        _record_universe_change("active_universe_fallback", "research_universe", active_universe, [], fallback_active=bool(STATE.get("fallback_mode_active")))

    max_new = int(os.getenv("MAX_NEW_ENTRIES_PER_TICK", "5"))
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    trend_direction = str(snap.get("trend_direction", "UNKNOWN") or "UNKNOWN").upper()
    STATE["last_trend_direction"] = trend_direction
    regime_u = regime
    if regime == "TRENDING":
        if trend_direction == "DOWN":
            regime_u = "TRENDING_DOWN"
        elif trend_direction == "UP":
            regime_u = "TRENDING_UP"
    prev_regime = str(STATE.get("last_regime") or "")
    STATE["last_regime"] = regime_u
    append_log("INFO", "MARKET", f"regime={regime_u} entry_mode={get_regime_entry_mode(regime_u)} trend_direction={trend_direction}")
    if _is_micro_mode_active() and prev_regime and prev_regime != regime_u:
        _deactivate_micro_mode("regime_changed")

    open_mode, open_metrics = get_opening_mode()
    STATE["opening_mode"] = open_mode
    STATE["opening_metrics"] = dict(open_metrics or {})
    if _in_open_filter_window():
        reason = str((open_metrics or {}).get("reason") or "n/a")
        conf_i = int((open_metrics or {}).get("confidence") or 0)
        action = {
            "OPEN_CLEAN": "NORMAL_TRADING",
            "OPEN_MODERATE": "REDUCED_TRADING",
            "OPEN_UNSAFE": "MICRO_TRADING",
            "OPEN_FEED_RETRY": "WAIT_RETRY",
            "OPEN_HARD_BLOCK": "BLOCK_ALL",
        }.get(open_mode, "NORMAL_TRADING")
        append_log("INFO", "OPEN", f"state={open_mode} reason={reason} action={action} confidence={conf_i}")
        if open_mode == "OPEN_FEED_RETRY":
            time.sleep(20)
            return
        if open_mode == "OPEN_HARD_BLOCK":
            append_log("WARN", "OPEN", f"state=OPEN_HARD_BLOCK reason={reason} action=BLOCK_ALL")
            _deactivate_micro_mode(reason)
            return

    selected_families = _maybe_refresh_active_strategy_families(regime_u, trend_direction, active_universe, research_universe)
    if not selected_families:
        selected_families = ["mean_reversion"]
        append_log("WARN", "ROUTE", "[ROUTE] no strategy met min score -> using micro fallback family=mean_reversion")

    opened = 0
    append_log("INFO", "UNIV", "scanning active universe")
    opened += _scan_top3_families(active_universe, selected_families, max_new=max_new, universe_source="active_universe")

    research_tail = [s for s in research_universe if s not in set(active_universe)]
    if opened <= 0:
        STATE["active_no_setup_cycles"] = int(STATE.get("active_no_setup_cycles") or 0) + 1
        append_log("INFO", "UNIV", "no setup in active universe → scanning research universe")
        _record_research_event("route_change", "route_scan=research_universe", active_top3=selected_families)
        opened += _scan_top3_families(research_tail, selected_families, max_new=max_new - opened, universe_source="research_universe")

    if regime_u == "SIDEWAYS":
        if opened <= 0 and "mean_reversion" in selected_families:
            STATE["mean_reversion_dry_cycles"] = int(STATE.get("mean_reversion_dry_cycles") or 0) + 1
            if int(STATE.get("mean_reversion_dry_cycles") or 0) >= 2:
                selected_families = [f for f in selected_families if f != "mean_reversion"] + ["mean_reversion"]
                append_log("INFO", "ROUTE", "SIDEWAYS mean_reversion dry -> allowing next-ranked family sooner")
        else:
            STATE["mean_reversion_dry_cycles"] = 0

    expand_cycles = int(getattr(CFG, "ACTIVE_UNIVERSE_EXPAND_CYCLES", 3) or 3)
    if opened <= 0 and int(STATE.get("active_no_setup_cycles") or 0) >= expand_cycles:
        append_log("INFO", "SCAN", "active universe weak → expanding scan scope")
        expanded = list(research_universe)
        _record_research_event("route_change", "route_scan=expanded_universe", active_top3=selected_families)
        opened += _scan_top3_families(expanded, selected_families, max_new=max_new - opened, universe_source="expanded_universe")

    if opened <= 0:
        STATE["no_entry_cycles"] = int(STATE.get("no_entry_cycles") or 0) + 1
        STATE["top3_dry_cycles"] = int(STATE.get("top3_dry_cycles") or 0) + 1
    else:
        STATE["no_entry_cycles"] = 0
        STATE["top3_dry_cycles"] = 0
        STATE["active_no_setup_cycles"] = 0
        STATE["fallback_mode_active"] = False

    dry_thr = int(getattr(CFG, "TOP3_DRY_CYCLE_THRESHOLD", 5) or 5)
    if int(STATE.get("top3_dry_cycles") or 0) >= dry_thr:
        append_log("WARN", "HEALTH", f"[HEALTH] top3 dry for {dry_thr} cycles -> recomputing")
        selected_families = _refresh_active_strategy_families(
            "top3_dry",
            regime_u,
            trend_direction,
            active_universe,
            research_universe,
        )
        append_log("INFO", "HEALTH", "[HEALTH] top3 dry -> expanding to fallback universe")

    trigger_n = int(getattr(CFG, "FALLBACK_TRIGGER_CYCLES", 5) or 5)
    if STATE.get("no_entry_cycles", 0) >= trigger_n:
        if not STATE.get("fallback_mode_active"):
            append_log("INFO", "UNIV", f"no tradable setup in primary for {trigger_n} cycles → activating fallback universe")
            build_fallback_universe()
            STATE["fallback_mode_active"] = True
            _record_research_event("fallback_activation", f"trigger_n={trigger_n}")

    if STATE.get("fallback_mode_active"):
        fb = list(STATE.get("fallback_universe") or [])
        if fb:
            append_log("INFO", "UNIV", f"scanning fallback universe size={len(fb)} strategy=TOP3")
            fb_opened = 0
            append_log("INFO", "ROUTE", "fallback_universe active -> top3 routing")
            _record_research_event("route_change", "route_scan=fallback_universe", active_top3=list(STATE.get("active_strategy_families") or selected_families))
            fb_opened += _scan_top3_families(fb, list(STATE.get("active_strategy_families") or selected_families), max_new=max_new, universe_source="fallback_universe")
            if fb_opened > 0:
                append_log(
                    "INFO",
                    "UNIV",
                    f"fallback entries allowed opened={fb_opened} size_multiplier={float(getattr(CFG, 'FALLBACK_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
                )
                STATE["no_entry_cycles"] = 0
            else:
                append_log("INFO", "UNIV", "fallback scanned but no eligible entries this cycle")

    _overfilter_health_check()
    _maybe_send_eod_report()


def run_loop_forever():
    append_log("INFO", "LOOP", "Trading loop started")
    append_log(
        "INFO",
        "MARKET",
        f"weak mode config top_n={int(getattr(CFG, 'WEAK_MARKET_TOP_N', 10) or 10)} "
        f"min_score={float(getattr(CFG, 'WEAK_MARKET_MIN_SCORE', 0.75) or 0.75):.2f} "
        f"size_multiplier={float(getattr(CFG, 'WEAK_MARKET_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
    )
    append_log(
        "INFO",
        "ROUTE",
        f"top3 selector min_score={int(getattr(CFG, 'STRATEGY_MIN_ACTIVE_SCORE', 40) or 40)} "
        f"refresh_min={int(getattr(CFG, 'STRATEGY_SELECTION_REFRESH_MINUTES', 10) or 10)} "
        f"dry_threshold={int(getattr(CFG, 'TOP3_DRY_CYCLE_THRESHOLD', 5) or 5)}",
    )
    append_log(
        "INFO",
        "OPEN",
        f"adaptive opening filter enabled unsafe_mult={float(getattr(CFG, 'OPEN_UNSAFE_SIZE_MULTIPLIER', 0.25) or 0.25):.2f} "
        f"moderate_mult={float(getattr(CFG, 'OPEN_MODERATE_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
    )
    if not _active_trade_universe():
        _load_research_universe_from_file()
    while True:
        try:
            tick()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(CFG.TICK_SECONDS))
