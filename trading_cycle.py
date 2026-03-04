import os
import time
from datetime import datetime, timedelta

import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log
from strategy_engine import generate_signal

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

EXCLUSIONS_FILE = getattr(CFG, "EXCLUSIONS_PATH", os.path.join(DATA_DIR, "exclusions.txt"))

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "open_trades": {},  # symbol -> trade dict
    "today_pnl": 0.0,
    "day_key": datetime.now().strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
    "wallet_net_inr": 0.0,
    "wallet_available_inr": 0.0,
    "daily_loss_cap_inr": float(getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0)),
    "daily_profit_milestone_inr": float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 90.0)),
    "profit_milestone_hit": False,
    "last_wallet_sync_ts": None,
}

RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.30")),
    "BUCKET_VALUE_INR": float(os.getenv("BUCKET_VALUE_INR", "500")),
    "MAX_EXPOSURE_PCT": float(os.getenv("MAX_EXPOSURE_PCT", "70")),
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


def _parse_hhmm(s):
    try:
        hh, mm = str(s).strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0


def _past_force_exit_time():
    now = datetime.now()
    fh, fm = _parse_hhmm(getattr(CFG, "FORCE_EXIT", "15:10"))
    cutoff = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return now >= cutoff


def _ensure_day_key():
    today = datetime.now().strftime("%Y-%m-%d")
    if STATE.get("day_key") != today:
        STATE["day_key"] = today
        STATE["today_pnl"] = 0.0
        STATE["open_trades"] = {}
        STATE["profit_milestone_hit"] = False
        append_log("INFO", "DAY", f"Auto rollover reset for {today}")


def set_runtime_param(key, value):
    RUNTIME[key] = value


def manual_reset_day():
    STATE["today_pnl"] = 0.0
    STATE["open_trades"] = {}
    STATE["day_key"] = datetime.now().strftime("%Y-%m-%d")
    STATE["profit_milestone_hit"] = False
    append_log("INFO", "DAY", "Manual day reset executed")
    return True


def is_live_enabled():
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))


def _load_exclusions_set():
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return {ln.strip().upper() for ln in f if ln.strip()}


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
    parts = [p.strip() for p in str(win_str).split(",") if p.strip()]
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


def _sync_wallet_and_caps(force=False):
    now = datetime.now()
    last = STATE.get("last_wallet_sync_ts")
    if not force and last and (now - last) < timedelta(seconds=120):
        return

    wallet_net = float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    wallet_avail = wallet_net

    # Wallet sync should not depend on LIVE initiation; /status must show broker wallet if token is valid.
    try:
        m = get_kite().margins() or {}
        eq = m.get("equity", {}) if isinstance(m, dict) else {}
        wallet_net = float(eq.get("net") or wallet_net or 0.0)
        avail = eq.get("available", {}) if isinstance(eq, dict) else {}
        if isinstance(avail, dict):
            wallet_avail = float(
                avail.get("live_balance")
                or avail.get("cash")
                or avail.get("opening_balance")
                or avail.get("adhoc_margin")
                or wallet_net
            )
        else:
            wallet_avail = wallet_net
    except Exception as e:
        append_log("WARN", "WALLET", f"Margins sync failed: {e}")

    STATE["wallet_net_inr"] = max(0.0, wallet_net)
    STATE["wallet_available_inr"] = max(0.0, wallet_avail)

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
    return len(STATE.get("open_trades") or {})


def _current_exposure_inr():
    return sum(float(t.get("entry", 0.0)) * int(t.get("qty", 0) or 0) for t in STATE.get("open_trades", {}).values())


def _max_exposure_inr():
    base = float(STATE.get("wallet_net_inr") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    return base * float(RUNTIME.get("MAX_EXPOSURE_PCT", 70.0)) / 100.0


def _calc_qty(price):
    wallet = float(STATE.get("wallet_available_inr") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    bucket = float(RUNTIME.get("BUCKET_VALUE_INR", 500.0) or 500.0)
    stop_pct = max(0.01, float(getattr(CFG, "STOPLOSS_PCT", 2.0)))
    risk_pct = max(0.01, float(getattr(CFG, "RISK_PER_TRADE_PCT", 1.0)))

    risk_amt = wallet * risk_pct / 100.0
    per_share_risk = price * stop_pct / 100.0
    risk_qty = int(risk_amt / per_share_risk) if per_share_risk > 0 else 1

    bucket_qty = int(bucket / price) if price > 0 else 0
    wallet_qty = int(wallet / price) if price > 0 else 0

    qty = max(1, min(risk_qty, bucket_qty if bucket_qty > 0 else 1, wallet_qty if wallet_qty > 0 else 1))
    append_log("INFO", "SIZE", f"Sizing price={price:.2f} wallet={wallet:.2f} bucket={bucket:.2f} qty={qty}")
    return qty


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


def _close_position(sym, reason="MANUAL", ltp_override=None):
    sym = (sym or "").strip().upper()
    trade = STATE.get("open_trades", {}).get(sym)
    if not trade:
        return False

    qty = int(trade.get("qty") or 0) or 1
    entry = float(trade.get("entry") or 0.0)
    exit_side = "SELL"

    ltp = float(ltp_override) if ltp_override is not None else None

    if not is_live_enabled():
        if ltp is None:
            ltp = entry
        pnl = (ltp - entry) * qty
        pnl_pct = ((ltp - entry) / entry * 100.0) if entry > 0 else 0.0
        STATE["today_pnl"] += pnl
        STATE["open_trades"].pop(sym, None)
        append_log("WARN", "EXIT", f"PAPER exit {sym} qty={qty} exit={ltp:.2f} pnl={pnl:.2f}({pnl_pct:.2f}%) reason={reason}")
        _notify(f"🔴 SELL {'LIVE' if is_live_enabled() else 'PAPER'}\n{sym} qty={qty}\nExit={ltp:.2f}\nPnL ₹{pnl:.2f} ({pnl_pct:.2f}%)\nReason={reason}")
        return True

    kite = get_kite()
    attempts = 3
    oid = None
    for attempt in range(1, attempts + 1):
        oid = _place_live_order(kite, sym, exit_side, qty)
        if oid:
            break
        append_log("WARN", "EXIT", f"LIVE exit retry {attempt}/{attempts} for {sym} ({reason})")
        time.sleep(0.6)

    if not oid:
        return False

    if ltp is None:
        ltp = _ltp(kite, sym) or entry
    pnl = (ltp - entry) * qty
    pnl_pct = ((ltp - entry) / entry * 100.0) if entry > 0 else 0.0
    STATE["today_pnl"] += pnl
    STATE["open_trades"].pop(sym, None)

    append_log("WARN", "EXIT", f"LIVE exit {sym} qty={qty} exit={ltp:.2f} pnl={pnl:.2f}({pnl_pct:.2f}%) reason={reason} oid={oid}")
    _notify(f"🔴 SELL {'LIVE' if is_live_enabled() else 'PAPER'}\n{sym} qty={qty}\nExit={ltp:.2f}\nPnL ₹{pnl:.2f} ({pnl_pct:.2f}%)\nReason={reason}")
    return True


def _close_all_open_trades(reason="MANUAL"):
    ok = True
    for sym in list(STATE.get("open_trades", {}).keys()):
        ok = _close_position(sym, reason=reason) and ok
    return ok


# compatibility for older bot call sites

def _close_open_trade(reason="MANUAL"):
    keys = list(STATE.get("open_trades", {}).keys())
    if not keys:
        return False
    return _close_position(keys[0], reason=reason)


def _manage_open_trades(force_only=False):
    if not STATE.get("open_trades"):
        return

    for sym, trade in list(STATE.get("open_trades", {}).items()):
        kite = get_kite() if is_live_enabled() else None
        ltp = _ltp(kite, sym) if kite else float(trade.get("entry") or 0.0)
        if ltp is None:
            append_log("WARN", "RISK", f"{sym} LTP unavailable for checks")
            continue

        if _past_force_exit_time():
            _close_position(sym, reason="TIME", ltp_override=ltp)
            continue

        if force_only:
            continue

        sl_price = float(trade.get("sl_price") or 0.0)
        if sl_price > 0 and ltp <= sl_price:
            _close_position(sym, reason="SL", ltp_override=ltp)
            continue

        entry = float(trade.get("entry") or 0.0)
        if entry <= 0:
            continue

        pnl_pct = ((ltp - entry) / entry) * 100.0
        peak_pnl = float(trade.get("peak_pnl_pct") or pnl_pct)
        if pnl_pct > peak_pnl:
            peak_pnl = pnl_pct
            trade["peak_pnl_pct"] = peak_pnl

        activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
        trail = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))
        buf = float(getattr(CFG, "BREAKEVEN_BUFFER_PCT", 0.15))
        if peak_pnl >= activate:
            threshold = peak_pnl - trail - buf
            if pnl_pct <= threshold:
                _close_position(sym, reason="TRAIL", ltp_override=ltp)
                continue

        append_log("INFO", "RISK", f"{sym} open pnl%={pnl_pct:.2f} peak%={peak_pnl:.2f} sl={sl_price:.2f}")


def _within_entry_window():
    now = datetime.now()
    sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
    eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def _can_open_new_trade(sym, entry):
    if sym in STATE.get("open_trades", {}):
        append_log("INFO", "ENTRY", f"Skip {sym}: already open")
        return False

    max_positions = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
    if _open_positions_count() >= max_positions:
        append_log("INFO", "ENTRY", f"Skip {sym}: max open positions reached ({max_positions})")
        return False

    wallet = float(STATE.get("wallet_available_inr") or 0.0)
    if entry > wallet:
        append_log("INFO", "ENTRY", f"Skip {sym}: cannot buy 1 share entry={entry:.2f} wallet={wallet:.2f}")
        return False

    next_exp = _current_exposure_inr() + entry
    max_exp = _max_exposure_inr()
    if max_exp > 0 and next_exp > max_exp:
        append_log("WARN", "ENTRY", f"Skip {sym}: exposure guard hit next={next_exp:.2f} max={max_exp:.2f}")
        return False

    return True


def _maybe_enter_from_signal(sig):
    if not sig:
        return False

    sym = sig["symbol"].strip().upper()
    entry = float(sig.get("entry") or 0.0)
    if entry <= 0:
        append_log("WARN", "TRADE", f"Invalid signal entry for {sym}: {entry}")
        return False

    if not _can_open_new_trade(sym, entry):
        return False

    qty = _calc_qty(entry)
    sl_price = entry * (1.0 - float(CFG.STOPLOSS_PCT) / 100.0)

    mode = "LIVE" if is_live_enabled() else "PAPER"
    oid = None

    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            if now_price > entry * (1.0 + max_slip):
                append_log("WARN", "SLIP", f"Skip {sym}: slip too high now={now_price} sig={entry}")
                return False

        oid = _place_live_order(kite, sym, "BUY", qty)
        if not oid:
            return False

    STATE["open_trades"][sym] = {
        "symbol": sym,
        "side": "BUY",
        "entry": entry,
        "qty": qty,
        "order_id": oid,
        "sl_price": sl_price,
        "peak_pnl_pct": 0.0,
        "opened_at": datetime.now().isoformat(timespec="seconds"),
    }

    append_log("INFO", "TRADE", f"{mode} BUY {sym} qty={qty} entry={entry:.2f} sl={sl_price:.2f} oid={oid}")
    _notify(f"🟢 BUY {mode}\n{sym} qty={qty}\nEntry={entry:.2f}")
    return True


def get_status_text():
    _ensure_day_key()
    _sync_wallet_and_caps(force=False)
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    uni_t = load_universe_trading()
    uni_l = load_universe_live()

    wallet = float(STATE.get("wallet_net_inr") or 0.0)
    avail = float(STATE.get("wallet_available_inr") or 0.0)
    exp = float(_current_exposure_inr())
    max_exp = float(_max_exposure_inr())

    open_rows = []
    for sym, t in sorted(STATE.get("open_trades", {}).items()):
        open_rows.append(f"- {sym} qty={int(t.get('qty') or 0)} entry={float(t.get('entry') or 0):.2f} sl={float(t.get('sl_price') or 0):.2f}")

    return (
        "📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(uni_t)} symbols\n"
        f"Universe(live): {len(uni_l)} symbols\n"
        f"Open Positions: {_open_positions_count()}\n"
        f"Today PnL: {float(STATE.get('today_pnl') or 0.0):.2f}\n\n"
        "Wallet/Caps:\n"
        f"- Wallet Net: ₹{wallet:.2f}\n"
        f"- Wallet Available: ₹{avail:.2f}\n"
        f"- Exposure: ₹{exp:.2f} / ₹{max_exp:.2f} ({RUNTIME.get('MAX_EXPOSURE_PCT')}%)\n"
        f"- Daily Loss Cap (hard): ₹{float(STATE.get('daily_loss_cap_inr') or 0.0):.2f}\n"
        f"- Profit Milestone (soft): ₹{float(STATE.get('daily_profit_milestone_inr') or 0.0):.2f}\n"
        f"- Bucket Size: ₹{float(RUNTIME.get('BUCKET_VALUE_INR') or 0.0):.2f}\n\n"
        "Open Trades:\n"
        + ("\n".join(open_rows) if open_rows else "(none)")
        + "\n\n"
        + f"AutoPromote: {getattr(CFG, 'AUTO_PROMOTE_ENABLED', False)} | Last: {STATE.get('last_promote_msg')}"
    )


def tick():
    _ensure_day_key()
    _sync_wallet_and_caps(force=False)

    # Force-exit must run even when paused.
    if STATE.get("open_trades") and _past_force_exit_time():
        append_log("WARN", "TIME", "Force-exit time reached; closing all open positions")
        _notify("⏰ FORCE EXIT time reached. Closing all open positions.")
        _manage_open_trades(force_only=True)

    if STATE.get("paused"):
        return

    daily_loss_cap = float(STATE.get("daily_loss_cap_inr") or 0.0)
    if daily_loss_cap > 0 and STATE["today_pnl"] <= -abs(daily_loss_cap):
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
            append_log("INFO", "CAP", "Profit target configured hard. Pausing loop.")
            return

    if (
        getattr(CFG, "AUTO_PROMOTE_ENABLED", False)
        and not STATE.get("open_trades")
        and _in_any_promote_window()
        and _cooldown_ok()
    ):
        if _market_stable():
            promote_universe(reason="AUTO_STABLE")
        else:
            STATE["last_promote_msg"] = "Skipped promote: market not stable"

    _manage_open_trades(force_only=False)

    if not _within_entry_window():
        return

    universe = load_universe_trading()
    if not universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return

    # Keep taking opportunities while wallet/exposure/buckets allow.
    max_new_entries_per_tick = int(os.getenv("MAX_NEW_ENTRIES_PER_TICK", "5"))
    opened = 0
    while opened < max_new_entries_per_tick:
        blocked_syms = set(STATE.get("open_trades", {}).keys())
        candidates = [s for s in universe if s not in blocked_syms]
        if not candidates:
            break

        append_log("INFO", "SCAN", f"Scanning candidates={len(candidates)} open={_open_positions_count()}")
        sig = generate_signal(candidates)
        if not sig:
            break

        entered = _maybe_enter_from_signal(sig)
        if not entered:
            break
        opened += 1


def run_loop_forever():
    append_log("INFO", "LOOP", "Trading loop started")
    while True:
        try:
            tick()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(CFG.TICK_SECONDS))
