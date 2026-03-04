import os
import time
from datetime import datetime, timedelta
import pandas as pd

import config as CFG
from log_store import append_log
from strategy_engine import generate_signal
from broker_zerodha import get_kite
from instrument_store import token_for_symbol

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

EXCLUSIONS_FILE = os.path.join(DATA_DIR, "exclusions.txt")

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "open_trade": None,
    "today_pnl": 0.0,
    "day_key": datetime.now().strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
    "peak": None,
}

RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.30")),
}

def _parse_hhmm(s):
    try:
        hh, mm = s.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0

def _ensure_day_key():
    today = datetime.now().strftime("%Y-%m-%d")
    if STATE.get("day_key") != today:
        STATE["day_key"] = today
        STATE["today_pnl"] = 0.0
        STATE["open_trade"] = None
        append_log("INFO", "DAY", f"Auto rollover reset for {today}")

def set_runtime_param(key, value):
    RUNTIME[key] = value

def manual_reset_day():
    STATE["today_pnl"] = 0.0
    STATE["open_trade"] = None
    STATE["day_key"] = datetime.now().strftime("%Y-%m-%d")
    append_log("INFO", "DAY", "Manual day reset executed")
    return True

def is_live_enabled():
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))

def _load_exclusions_set():
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])

def _save_exclusions_set(s):
    with open(EXCLUSIONS_FILE, "w") as f:
        for sym in sorted(s):
            f.write(sym + "\n")

def list_exclusions():
    s = _load_exclusions_set()
    if not s:
        return "✅ Excluded symbols: (none)"
    return "⛔ Excluded symbols:\n" + "\n".join(sorted(s))

def exclude_symbol(sym):
    sym = sym.strip().upper()
    if not sym:
        return "Usage: /exclude SYMBOL"
    s = _load_exclusions_set()
    s.add(sym)
    _save_exclusions_set(s)
    append_log("WARN", "EXCL", f"Excluded {sym}")
    return f"⛔ {sym} excluded permanently. (/include {sym} to release)"

def include_symbol(sym):
    sym = sym.strip().upper()
    if not sym:
        return "Usage: /include SYMBOL"
    s = _load_exclusions_set()
    if sym in s:
        s.remove(sym)
        _save_exclusions_set(s)
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
    excl = _load_exclusions_set()
    syms = [s for s in syms if s not in excl]

    try:
        maxn = int(getattr(CFG, "UNIVERSE_SIZE", 30))
        syms = syms[:maxn]
    except Exception:
        pass
    return syms

def load_universe_live():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = _load_exclusions_set()
    return [s for s in syms if s not in excl]

def _parse_windows(win_str):
    windows = []
    if not win_str:
        return windows
    parts = [p.strip() for p in win_str.split(",") if p.strip()]
    for p in parts:
        if "-" not in p:
            continue
        a, b = p.split("-", 1)
        ah, am = _parse_hhmm(a)
        bh, bm = _parse_hhmm(b)
        windows.append(((ah, am), (bh, bm)))
    return windows

def _in_any_promote_window():
    now = datetime.now()
    w = _parse_windows(getattr(CFG, "PROMOTE_WINDOWS", ""))
    if not w:
        return False
    for (ah, am), (bh, bm) in w:
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
    return (datetime.now() - last) >= timedelta(minutes=cd_min)

def _top10_overlap_ratio(a, b):
    a10 = a[:10]
    b10 = b[:10]
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

        rng = (tail["high"] - tail["low"]).astype(float)
        close = tail["close"].astype(float)
        rng_pct = (rng / close) * 100.0
        avg_rng_pct = float(rng_pct.mean())

        max_ok = float(getattr(CFG, "STABILITY_ATR_PCT_MAX", 0.35))
        return avg_rng_pct <= max_ok
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
        STATE["last_promote_ts"] = datetime.now()
        STATE["last_promote_msg"] = f"Promoted ({reason}) overlap={overlap:.2f}"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
        return True

    STATE["last_promote_msg"] = "Promote failed (copy)"
    return False


def _safe_universe(u):
    # Avoid NoneType iterable crash
    try:
        return u if u is not None else []
    except Exception:
        return []


def get_status_text():
    _ensure_day_key()
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    uni_t = load_universe_trading()
    uni_l = load_universe_live()

    return (
        "📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(uni_t)} symbols\n"
        f"Universe(live): {len(uni_l)} symbols\n"
        f"Today PnL: {float(STATE.get('today_pnl') or 0.0):.2f}\n"
        f"Open Trade: {STATE.get('open_trade')}\n\n"
        "Caps:\n"
        f"- Daily Loss Cap: {CFG.DAILY_LOSS_CAP_INR}\n"
        f"- Daily Profit Target: {CFG.DAILY_PROFIT_TARGET_INR}\n"
        f"- Stoploss %: {CFG.STOPLOSS_PCT}\n"
        f"- Risk/Trade %: {CFG.RISK_PER_TRADE_PCT}\n"
        f"- Tick Seconds: {CFG.TICK_SECONDS}\n"
        f"- Max Slippage %: {RUNTIME.get('MAX_ENTRY_SLIPPAGE_PCT')}\n\n"
        f"AutoPromote: {getattr(CFG, 'AUTO_PROMOTE_ENABLED', False)} | Last: {STATE.get('last_promote_msg')}\n"
    )

def _calc_qty(price):
    capital = float(CFG.CAPITAL_INR)
    risk_amt = capital * float(CFG.RISK_PER_TRADE_PCT) / 100.0
    per_share_risk = price * float(CFG.STOPLOSS_PCT) / 100.0
    if per_share_risk <= 0:
        return 1
    risk_qty = int(risk_amt / per_share_risk)
    affordable_qty = int(capital / price) if price > 0 else 0
    return max(1, min(risk_qty, affordable_qty))

def _ltp(kite, sym):
    try:
        ins = f"{CFG.EXCHANGE}:{sym}"
        data = kite.ltp([ins])
        return float(data[ins]["last_price"])
    except Exception:
        return None

def _place_live_order(kite, sym, side, qty):
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=CFG.EXCHANGE,
            tradingsymbol=sym,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=CFG.PRODUCT,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        return order_id
    except Exception as e:
        append_log("ERROR", "ORDER", f"Order failed {sym} {side} qty={qty}: {e}")
        return None

def _close_open_trade(reason="MANUAL"):
    trade = STATE.get("open_trade")
    if not trade:
        return False
    sym = trade.get("symbol")
    side = trade.get("side")
    qty = int(trade.get("qty") or 0) or 1
    exit_side = "SELL" if side == "BUY" else "BUY"

    if not is_live_enabled():
        append_log("WARN", "EXIT", f"PAPER exit {sym} ({reason})")
        STATE["open_trade"] = None
        return True

    kite = get_kite()
    oid = _place_live_order(kite, sym, exit_side, qty)
    if oid:
        append_log("WARN", "EXIT", f"LIVE exit {sym} ({reason}) order_id={oid}")
        STATE["open_trade"] = None
        return True
    return False

def _within_entry_window():
    now = datetime.now()
    sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
    eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end

def tick():
    from log_store import append_log
    append_log("INFO", "CYCLE", "tick() entered")
    append_log("INFO", "TICK", "Tick running")
    if STATE.get("paused"):
        append_log("INFO", "TICK", "Paused=True (use /startloop)")
        return
    _ensure_day_key()
    if STATE.get("paused"):
        return

    if STATE["today_pnl"] <= -abs(CFG.DAILY_LOSS_CAP_INR):
        append_log("WARN", "CAP", "Daily loss cap hit. Pausing loop.")
        STATE["paused"] = True
        return

    if STATE["today_pnl"] >= abs(CFG.DAILY_PROFIT_TARGET_INR):
        append_log("INFO", "CAP", "Daily profit target hit. Pausing loop.")
        STATE["paused"] = True
        return

    if (
        getattr(CFG, "AUTO_PROMOTE_ENABLED", False)
        and STATE.get("open_trade") is None
        and _in_any_promote_window()
        and _cooldown_ok()
    ):
        if _market_stable():
            promote_universe(reason="AUTO_STABLE")
        else:
            STATE["last_promote_msg"] = "Skipped promote: market not stable"

    if not _within_entry_window():
        return

    if STATE.get("open_trade"):
        return

    universe = load_universe_trading()
    if not universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return

    sig = generate_signal(universe)
    if not sig:
        return

    sym = sig["symbol"].strip().upper()
    entry = float(sig.get("entry") or 0.0)
    if entry <= 0:
        append_log("WARN", "TRADE", f"Invalid signal entry for {sym}: {entry}")
        return

    qty = _calc_qty(entry)
    sl_price = entry * (1.0 - float(CFG.STOPLOSS_PCT) / 100.0)

    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            sig_price = entry
            if sig_price <= 0:
                append_log("WARN", "SLIP", f"Skip {sym}: invalid sig price {sig_price}")
                return
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            if now_price > sig_price * (1.0 + max_slip):
                append_log("WARN", "SLIP", f"Skip {sym}: slip too high now={now_price} sig={sig_price}")
                return

        oid = _place_live_order(kite, sym, "BUY", qty)
        if not oid:
            return

        STATE["open_trade"] = {
            "symbol": sym,
            "side": "BUY",
            "entry": entry,
            "qty": qty,
            "order_id": oid,
            "sl_price": sl_price,
            "peak": entry,
        }
        append_log("INFO", "TRADE", f"LIVE Entered {sym} qty={qty} entry={entry} sl={sl_price} oid={oid}")
    else:
        STATE["open_trade"] = {
            "symbol": sym,
            "side": "BUY",
            "entry": entry,
            "qty": qty,
            "order_id": None,
            "sl_price": sl_price,
            "peak": entry,
        }
        append_log("INFO", "TRADE", f"PAPER Entered {sym} qty={qty} entry={entry} sl={sl_price}")

def run_loop_forever():
    append_log("INFO", "LOOP", "Trading loop started")
    while True:
        try:
            tick()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(CFG.TICK_SECONDS))

def _check_restart_flag():
    try:
        flag_path = getattr(CFG, "RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")
        return os.path.exists(flag_path), flag_path
    except Exception:
        return False, "/home/ubuntu/trident-bot/RESTART_REQUIRED"
    try:
        universe = _safe_universe(locals().get('universe'))
    except Exception:
        pass

