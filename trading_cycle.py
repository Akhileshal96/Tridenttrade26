import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log
from strategy_engine import generate_signal
from excluded_store import load_excluded, add_symbol, remove_symbol
from execution_engine import monitor_positions as ee_monitor_positions, process_entries as ee_process_entries, force_exit_all as ee_force_exit_all
import risk_engine as RISK
import research_engine as RE
from universe_builder import SECTOR_MAP
from market_regime import get_market_regime_snapshot, get_regime_entry_mode

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
    "no_entry_cycles": 0,
    "fallback_mode_active": False,
}

# backwards compatibility for any caller that still checks open_trades key
STATE["open_trades"] = STATE["positions"]

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
        append_log("INFO", "DAY", f"Auto rollover reset for {today}")


def set_runtime_param(key, value):
    RUNTIME[key] = value


def manual_reset_day():
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
    STATE["cooldown_until"] = datetime.now(IST) + timedelta(seconds=sec)


def _apply_skip_cooldown(sym: str, reason: str, minutes: int = 3):
    sym = (sym or "").strip().upper()
    if not sym:
        return
    until = datetime.now(IST) + timedelta(minutes=max(1, int(minutes)))
    STATE.setdefault("skip_cooldown", {})[sym] = until
    append_log("INFO", "SKIP", f"{sym} cooldown applied reason={reason}")


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
        STATE["today_pnl"] += pnl
        RISK.update_loss_streak(STATE, pnl)
        RISK.check_day_drawdown_guard(STATE)
        _positions().pop(sym, None)
        STATE["last_exit_ts"][sym] = datetime.now(IST)
        _set_cooldown()
        append_log("WARN", "EXIT", f"{sym} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
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
    STATE["today_pnl"] += pnl
    RISK.update_loss_streak(STATE, pnl)
    RISK.check_day_drawdown_guard(STATE)
    _positions().pop(sym, None)
    STATE["last_exit_ts"][sym] = datetime.now(IST)
    _set_cooldown()
    append_log("WARN", "EXIT", f"{sym} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
    _notify(f"🔴 {close_side} LIVE\nSymbol: {sym}\nExit: {ltp:.2f}\nPnL ₹: {pnl:.2f}\nPnL %: {pnl_pct:.2f}%\nReason: {reason}")
    return True


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

    if STATE.get("cooldown_until") and now < STATE["cooldown_until"]:
        append_log("INFO", "SKIP", f"{sym} reason=cooldown")
        return False

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    last_exit = STATE.get("last_exit_ts", {}).get(sym)
    reentry_block = int(getattr(CFG, "REENTRY_BLOCK_MINUTES", 30))
    if last_exit and (now - last_exit) < timedelta(minutes=reentry_block):
        if not momentum_positive:
            append_log("INFO", "SKIP", f"{sym} reason=reentry_block")
            return False
        append_log("INFO", "SKIP", f"{sym} reentry_block bypassed reason=positive_momentum")

    if STATE.get("halt_for_day"):
        append_log("INFO", "SKIP", f"{sym} reason=halt_for_day")
        return False

    pause_until = STATE.get("pause_entries_until")
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
    wallet_avail = float(STATE.get("wallet_available_inr") or 0.0)
    if required_value > wallet_avail:
        append_log("INFO", "SKIP", f"{sym} reason=insufficient_wallet need={required_value:.2f} avail={wallet_avail:.2f}")
        return False

    if not RISK.can_enter_trade(sym, float(entry), _positions(), float(STATE.get("wallet_net_inr") or 0.0), int(qty), sector=_sector_for_symbol(sym)):
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
        return dyn

    from_file = _load_research_universe_from_file()
    if from_file:
        append_log("INFO", "UNIV", f"Research universe loaded size={len(from_file)}")
        return from_file

    fallback = load_universe_trading()
    if fallback:
        append_log("INFO", "UNIV", f"Fallback static size={len(fallback)}")
    return fallback


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
    if rg == "WEAK":
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


def confirm_long_htf(symbol: str) -> bool:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return True
    df = _htf_fetch(symbol)
    if df.empty or "close" not in df.columns:
        append_log("INFO", "CONFIRM", f"BUY blocked {symbol} reason=htf_not_bullish")
        return False
    ma_n = int(getattr(CFG, "HTF_CONFIRM_MA", 20) or 20)
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean().dropna()
    if len(ma) < 2:
        append_log("INFO", "CONFIRM", f"BUY blocked {symbol} reason=htf_not_bullish")
        return False
    ok = float(close.iloc[-1]) > float(ma.iloc[-1]) and float(ma.iloc[-1]) > float(ma.iloc[-2])
    if ok and bool(getattr(CFG, "HTF_CONFIRM_RSI", False)):
        rsi = _calc_rsi(close)
        min_rsi = float(getattr(CFG, "HTF_LONG_MIN_RSI", 52.0) or 52.0)
        ok = float(rsi.iloc[-1]) >= min_rsi
    if not ok:
        append_log("INFO", "CONFIRM", f"BUY blocked {symbol} reason=htf_not_bullish")
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


def generate_short_signal(symbol: str):
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
    rs_ok = (rs_vs_nifty is None) or (float(rs_vs_nifty) <= max_rs_short)
    if not (price < sma20 and sma20 < sma20_prev and vol_score > float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2) and rs_ok):
        return None
    return {"symbol": sym, "entry": price, "side": "SHORT", "volume_score": vol_score, "rs_vs_nifty": rs_vs_nifty}


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
    sym = str(sig.get("symbol") or "").strip().upper()
    entry = float(sig.get("entry") or 0.0)
    if not sym or entry <= 0:
        return False

    if _open_short_positions_count() >= int(getattr(CFG, "MAX_SHORT_POSITIONS", 2) or 2):
        append_log("WARN", "RISK", "max short positions reached")
        return False

    if not confirm_short_htf(sym):
        return False

    qty, bucket_qty, risk_qty = _calc_qty(sym, entry)
    mult = float(getattr(CFG, "SHORT_SIZE_MULTIPLIER", 0.5) or 0.5)
    qty = max(1, int(math.floor(qty * mult))) if qty > 0 else 0
    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
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
        oid = _place_live_order(kite, sym, "SELL", qty)
        if not oid:
            append_log("INFO", "SKIP", f"{sym} reason=order_failed")
            return False

    _positions()[sym] = {
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
        "sector": _sector_for_symbol(sym),
    }
    _set_cooldown()
    append_log("INFO", "ENTRY", f"SHORT {sym} qty={qty} entry={booked_entry:.2f}")
    _notify(f"🟠 SHORT {mode}\nSymbol: {sym}\nQuantity: {qty}\nEntry: {booked_entry:.2f}")
    return True

def _maybe_enter_from_signal(sig):
    if not sig:
        return False
    sym = sig["symbol"].strip().upper()
    append_log("INFO", "SCAN", f"Scanning {sym}")

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    # 1) Market regime check (requested before buy gating)
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    research_universe = _active_trade_universe()
    allowed, reason, meta = is_market_entry_allowed(sym, regime, research_universe)

    if regime == "WEAK":
        if not allowed:
            append_log("WARN", "MARKET", f"regime=WEAK → blocked {sym} reason={reason}")
            weak_cd = int(getattr(CFG, "MARKET_WEAK_COOLDOWN_MIN", 3) or 3)
            weak_cd = max(2, min(5, weak_cd))
            _apply_skip_cooldown(sym, "market_weak", minutes=weak_cd)
            return False
        append_log("INFO", "MARKET", f"regime=WEAK → selective entry allowed for {sym} rank={meta.get('rank')} score={float(meta.get('score') or 0.0):.2f}")
    elif regime == "UNKNOWN" and bool(getattr(CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", False)):
        append_log("WARN", "MARKET", f"regime=UNKNOWN → blocked {sym} reason=unknown_regime")
        return False

    if not confirm_long_htf(sym):
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
    if regime == "WEAK":
        mult = float(getattr(CFG, "WEAK_MARKET_SIZE_MULTIPLIER", 0.5) or 0.5)
        if mult > 0:
            reduced = max(1, int(math.floor(qty * mult)))
            if reduced < qty:
                qty = reduced
                append_log("INFO", "MARKET", f"regime=WEAK → size reduced multiplier={mult}")
    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
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
        oid = _place_live_order(kite, sym, "BUY", qty)
        if not oid:
            append_log("INFO", "SKIP", f"{sym} reason=order_failed")
            return False

    _positions()[sym] = {
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
        "sector": _sector_for_symbol(sym),
    }
    _set_cooldown()
    append_log("INFO", "SIG", f"BUY trigger {sym}")
    append_log("INFO", "TRADE", f"{mode} BUY {sym} qty={qty}")
    _notify(
        f"🟢 BUY {mode}\n"
        f"Symbol: {sym}\n"
        f"Quantity: {qty}\n"
        f"Entry: {booked_entry:.2f}\n"
        f"Wallet: {float(STATE.get('wallet_net_inr') or 0.0):.2f}"
    )
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


def get_status_text():
    _ensure_day_key()
    RISK.sync_wallet(STATE)
    _sync_wallet_and_caps(force=False)
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    rows = []
    for sym, p in sorted(_positions().items()):
        e, q = _trade_entry_qty(p)
        rows.append(f"- {sym} qty={q} entry={e:.2f}")
    current_profit, current_loss = _current_open_pnl_breakdown()
    return (
        "📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(load_universe_trading())} symbols\n"
        f"Universe(live): {len(load_universe_live())} symbols\n"
        f"Open Positions: {_open_positions_count()}\n"
        f"Today PnL: {float(STATE.get('today_pnl') or 0.0):.2f}\n"
        f"Current Profit (open): ₹{current_profit:.2f}\n"
        f"Current Loss (open): ₹{current_loss:.2f}\n\n"
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




def _scan_short_entries(universe: list, max_new: int) -> int:
    opened = 0
    held = set(_positions().keys())
    for sym in [s for s in universe if s not in held][: max_new * 2]:
        if opened >= max_new:
            break
        append_log("INFO", "SCAN", f"Scanning {sym}")
        sig = generate_short_signal(sym)
        if not sig:
            continue
        if _maybe_enter_short_from_signal(sig):
            opened += 1
    return opened


def _scan_long_entries(universe: list, max_new: int) -> int:
    before = _open_positions_count()
    ee_process_entries(
        universe,
        _positions(),
        signal_fn=generate_signal,
        try_enter_fn=_maybe_enter_from_signal,
        max_new=max_new,
    )
    return max(0, _open_positions_count() - before)

def tick():
    _ensure_day_key()
    RISK.sync_wallet(STATE)
    _sync_wallet_and_caps(force=False)
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

    universe = _resolve_trade_universe()
    if not universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return

    max_new = int(os.getenv("MAX_NEW_ENTRIES_PER_TICK", "5"))
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    append_log("INFO", "MARKET", f"regime={regime} entry_mode={get_regime_entry_mode(regime)}")

    opened = 0
    if regime == "WEAK" and bool(getattr(CFG, "ENABLE_SHORT_MODE", True)):
        append_log("INFO", "MARKET", "regime=WEAK → activating SHORT mode")
        opened += _scan_short_entries(universe, max_new=max_new)
        if opened < max_new:
            opened += _scan_long_entries(universe, max_new=max_new - opened)
    else:
        opened += _scan_long_entries(universe, max_new=max_new)

    if opened <= 0:
        STATE["no_entry_cycles"] = int(STATE.get("no_entry_cycles") or 0) + 1
    else:
        STATE["no_entry_cycles"] = 0
        STATE["fallback_mode_active"] = False

    trigger_n = int(getattr(CFG, "FALLBACK_TRIGGER_CYCLES", 5) or 5)
    if STATE.get("no_entry_cycles", 0) >= trigger_n:
        if not STATE.get("fallback_mode_active"):
            append_log("INFO", "UNIV", f"no tradable setup in primary for {trigger_n} cycles → activating fallback universe")
            build_fallback_universe()
            STATE["fallback_mode_active"] = True

    if STATE.get("fallback_mode_active"):
        fb = list(STATE.get("fallback_universe") or [])
        if fb:
            append_log("INFO", "UNIV", f"scanning fallback universe size={len(fb)}")
            fb_opened = 0
            if regime == "WEAK" and bool(getattr(CFG, "ENABLE_SHORT_MODE", True)):
                fb_opened += _scan_short_entries(fb, max_new=max_new)
                if fb_opened < max_new:
                    fb_opened += _scan_long_entries(fb, max_new=max_new - fb_opened)
            else:
                fb_opened += _scan_long_entries(fb, max_new=max_new)
            if fb_opened > 0:
                append_log(
                    "INFO",
                    "UNIV",
                    f"fallback entries allowed opened={fb_opened} size_multiplier={float(getattr(CFG, 'FALLBACK_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
                )
                STATE["no_entry_cycles"] = 0
            else:
                append_log("INFO", "UNIV", "fallback scanned but no eligible entries this cycle")


def run_loop_forever():
    append_log("INFO", "LOOP", "Trading loop started")
    append_log(
        "INFO",
        "MARKET",
        f"weak mode config top_n={int(getattr(CFG, 'WEAK_MARKET_TOP_N', 10) or 10)} "
        f"min_score={float(getattr(CFG, 'WEAK_MARKET_MIN_SCORE', 0.75) or 0.75):.2f} "
        f"size_multiplier={float(getattr(CFG, 'WEAK_MARKET_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
    )
    if not _active_trade_universe():
        _load_research_universe_from_file()
    while True:
        try:
            tick()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(CFG.TICK_SECONDS))
