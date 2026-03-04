import os
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pytz

import config as CFG
from log_store import append_log
from broker_zerodha import get_kite

# Optional imports (keep compatibility if your repo has them)
try:
    from strategy_engine import generate_signal  # expected: generate_signal(universe)->dict|None
except Exception:
    generate_signal = None  # type: ignore

IST = pytz.timezone("Asia/Kolkata")

# -----------------------------
# State
# -----------------------------
STATE: Dict[str, Any] = {
    "paused": False,             # loop paused
    "open_trade": None,          # dict with symbol/side/qty/price/entry_time_ist
    "today_pnl": 0.0,            # your bot-computed pnl (optional)
    "day_key": None,             # YYYY-MM-DD
    "peak_pct": None,            # profit lock: peak pnl% since entry
    "initiated": False,          # set True by /initiate (if your bot uses this)
    "live_override": False,      # set True by /initiate (recommended)
    "last_tick_ist": None,
    "last_signal": None,
    "last_error": None,
}

RUNTIME: Dict[str, Any] = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.7")),  # optional
}

# -----------------------------
# Time helpers (IST)
# -----------------------------
def now_ist() -> datetime:
    return datetime.now(IST)

def _day_key() -> str:
    return now_ist().strftime("%Y-%m-%d")

def _parse_hhmm(val: str) -> Tuple[int, int]:
    h, m = val.strip().split(":")
    return int(h), int(m)

def _within_entry_window() -> bool:
    n = now_ist()
    start_s = getattr(CFG, "ENTRY_START", "09:20")
    end_s = getattr(CFG, "ENTRY_END", "14:30")
    sh, sm = _parse_hhmm(start_s)
    eh, em = _parse_hhmm(end_s)
    start = n.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = n.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= n <= end

def _past_force_exit_time() -> bool:
    # default 15:10 IST (configurable)
    n = now_ist()
    t = getattr(CFG, "FORCE_EXIT_TIME", "15:10")
    hh, mm = _parse_hhmm(t)
    cutoff = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return n >= cutoff

# -----------------------------
# Mode helper (LIVE vs PAPER)
# -----------------------------
def is_live_enabled() -> bool:
    # Runtime override from Telegram commands
    if STATE.get("live_override"):
        return True
    if STATE.get("initiated"):
        return True
    return bool(getattr(CFG, "IS_LIVE", False))

def set_live_override(enable: bool) -> None:
    STATE["live_override"] = bool(enable)
    STATE["initiated"] = bool(enable)

# -----------------------------
# Restart flag (systemd auto-restart)
# -----------------------------
def _restart_flag_path() -> str:
    return getattr(CFG, "RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")

def restart_requested() -> bool:
    return os.path.exists(_restart_flag_path())

def clear_restart_flag() -> None:
    p = _restart_flag_path()
    if os.path.exists(p):
        os.remove(p)

# -----------------------------
# Order helpers
# -----------------------------
def _place_live_order(kite, symbol: str, side: str, qty: int) -> Optional[str]:
    """
    side: 'BUY' or 'SELL'
    """
    try:
        order_id = kite.place_order(
            variety="regular",
            exchange=getattr(CFG, "EXCHANGE", "NSE"),
            tradingsymbol=symbol,
            transaction_type=side,
            quantity=int(qty),
            order_type="MARKET",
            product=getattr(CFG, "PRODUCT", "MIS"),
        )
        return str(order_id) if order_id else None
    except Exception as e:
        append_log("ERROR", "ORDER", f"place_order failed {symbol} {side} qty={qty} err={e}")
        STATE["last_error"] = str(e)
        return None

def _ltp(kite, symbol: str) -> Optional[float]:
    try:
        exch = getattr(CFG, "EXCHANGE", "NSE")
        q = f"{exch}:{symbol}"
        data = kite.ltp([q])
        return float(data[q]["last_price"])
    except Exception as e:
        append_log("ERROR", "TICK", f"ltp failed {symbol} err={e}")
        STATE["last_error"] = str(e)
        return None

# -----------------------------
# Trade lifecycle
# -----------------------------
def _ensure_day_rollover() -> None:
    dk = _day_key()
    if STATE.get("day_key") != dk:
        STATE["day_key"] = dk
        STATE["today_pnl"] = 0.0
        STATE["peak_pct"] = None
        append_log("INFO", "DAY", f"New day {dk} (reset today_pnl/peak)")

def _open_trade(symbol: str, side: str, qty: int, entry_price: float) -> None:
    STATE["open_trade"] = {
        "symbol": symbol,
        "side": side,
        "qty": int(qty),
        "price": float(entry_price),
        "entry_time_ist": now_ist().isoformat(),
    }
    STATE["peak_pct"] = None

def _close_trade_state() -> None:
    STATE["open_trade"] = None
    STATE["peak_pct"] = None

def _pnl_pct(entry: float, ltp: float, side: str) -> float:
    if entry <= 0:
        return 0.0
    raw = (ltp - entry) / entry * 100.0
    return raw if side.upper() == "BUY" else -raw

def exit_trade(reason: str) -> bool:
    trade = STATE.get("open_trade")
    if not trade:
        return False

    sym = trade["symbol"]
    side = trade["side"].upper()
    qty = int(trade.get("qty", 1))
    exit_side = "SELL" if side == "BUY" else "BUY"

    if not is_live_enabled():
        append_log("WARN", "EXIT", f"PAPER exit {sym} ({reason})")
        _close_trade_state()
        return True

    kite = get_kite()
    oid = _place_live_order(kite, sym, exit_side, qty)
    if oid:
        append_log("WARN", "EXIT", f"LIVE exit {sym} ({reason}) order_id={oid}")
        _close_trade_state()
        return True

    append_log("ERROR", "EXIT", f"Exit failed {sym} ({reason})")
    return False

# -----------------------------
# Risk / exit checks
# -----------------------------
def _check_stoploss(ltp: float) -> None:
    trade = STATE.get("open_trade")
    if not trade:
        return
    entry = float(trade["price"])
    side = trade["side"].upper()

    sl_pct = float(getattr(CFG, "STOPLOSS_PCT", 2.0))  # % loss
    pnl = _pnl_pct(entry, ltp, side)
    if pnl <= -abs(sl_pct):
        exit_trade(f"Stoploss hit ({pnl:.2f}%)")

def _check_profit_lock(ltp: float) -> None:
    trade = STATE.get("open_trade")
    if not trade:
        return

    entry = float(trade["price"])
    side = trade["side"].upper()

    activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
    trail = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))

    pnl = _pnl_pct(entry, ltp, side)

    if STATE["peak_pct"] is None:
        STATE["peak_pct"] = pnl
    else:
        STATE["peak_pct"] = max(float(STATE["peak_pct"]), pnl)

    peak = float(STATE["peak_pct"])

    if peak >= activate:
        lock_floor = peak - trail
        # If price falls back below trailing floor -> exit
        if pnl <= lock_floor:
            append_log("WARN", "LOCK", f"Profit lock trail: peak={peak:.2f}% pnl={pnl:.2f}% floor={lock_floor:.2f}%")
            exit_trade("Profit lock trail")

def _check_force_exit() -> None:
    if STATE.get("open_trade") and _past_force_exit_time():
        exit_trade("Force exit time reached")

# -----------------------------
# Status text (used by /status)
# -----------------------------
def get_status_text() -> str:
    n = now_ist()
    mode = "LIVE" if is_live_enabled() else "PAPER"
    paused = bool(STATE.get("paused"))
    tick_s = int(getattr(CFG, "TICK_SECONDS", 20))
    entry_start = getattr(CFG, "ENTRY_START", "09:20")
    entry_end = getattr(CFG, "ENTRY_END", "14:30")
    force_exit_t = getattr(CFG, "FORCE_EXIT_TIME", "15:10")

    lines = []
    lines.append(f"🕒 Time (IST): {n.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"⚙️ Mode: {mode} | Engine: RUNNING | Paused: {paused}")
    lines.append(f"⏱ Tick: {tick_s}s | Entry window: {entry_start}–{entry_end} | Force exit: {force_exit_t}")
    lines.append(f"📈 Today PnL (bot): {float(STATE.get('today_pnl', 0.0)):.2f} INR")

    trade = STATE.get("open_trade")
    if not trade:
        lines.append("📊 Open trade: None")
    else:
        sym = trade["symbol"]
        side = trade["side"]
        qty = trade.get("qty", 1)
        entry = float(trade["price"])

        ltp_txt = "NA"
        pnl_txt = "NA"
        try:
            kite = get_kite()
            ltp = _ltp(kite, sym)
            if ltp is not None:
                pnlp = _pnl_pct(entry, ltp, str(side))
                ltp_txt = f"{ltp:.2f}"
                pnl_txt = f"{pnlp:.2f}%"
        except Exception:
            pass

        lines.append(f"📊 Open trade: {sym} | side={side} | qty={qty}")
        lines.append(f"💰 Entry: {entry:.2f} | LTP: {ltp_txt} | PnL%: {pnl_txt}")
        lines.append(f"🧾 Entry time: {trade.get('entry_time_ist','NA')}")

        peak = STATE.get("peak_pct")
        peak_s = "NA" if peak is None else f"{float(peak):.2f}%"
        act = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
        trl = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))
        lines.append(f"🔒 Profit lock: peak={peak_s} | activates at {act:.2f}% | trail {trl:.2f}%")

    if STATE.get("last_error"):
        lines.append(f"⚠️ Last error: {STATE['last_error']}")

    return "\n".join(lines)

# -----------------------------
# Tick + loop
# -----------------------------
def tick() -> None:
    """
    One cycle tick.
    - checks restart flag
    - updates day rollover
    - handles forced exits
    - manages open trade exits (SL + profit lock)
    - generates new signals & enters only within entry window
    """
    if restart_requested():
        append_log("WARN", "RESTART", "Restart flag detected. Exiting loop for systemd restart.")
        raise SystemExit(0)

    _ensure_day_rollover()
    STATE["last_tick_ist"] = now_ist().isoformat()

    if STATE.get("paused"):
        append_log("INFO", "TICK", "Paused=True (use /startloop to resume)")
        return

    # 1) Forced exit time
    _check_force_exit()

    # 2) If trade open -> manage exits
    trade = STATE.get("open_trade")
    if trade:
        sym = trade["symbol"]
        kite = get_kite()
        ltp = _ltp(kite, sym)
        if ltp is None:
            return
        _check_stoploss(ltp)
        # trade might have closed by stoploss
        if STATE.get("open_trade"):
            _check_profit_lock(ltp)
        return

    # 3) No trade open -> only enter within entry window
    if not _within_entry_window():
        append_log("INFO", "TICK", "Outside entry window. No new entries.")
        return

    if generate_signal is None:
        append_log("ERROR", "SIG", "strategy_engine.generate_signal not available")
        return

    try:
        # Universe should come from your strategy_engine internally, OR you can pass in CFG.UNIVERSE if exists.
        universe = getattr(CFG, "UNIVERSE", None)
        sig = generate_signal(universe)
        STATE["last_signal"] = sig

        if not sig:
            append_log("INFO", "SIG", "No signal")
            return

        symbol = sig.get("symbol") or sig.get("tradingsymbol")
        side = (sig.get("side") or "BUY").upper()
        qty = int(sig.get("qty") or 1)

        if not symbol:
            append_log("WARN", "SIG", f"Signal missing symbol: {sig}")
            return

        kite = get_kite()
        ltp = _ltp(kite, symbol)
        if ltp is None:
            return

        if not is_live_enabled():
            append_log("INFO", "ORDER", f"PAPER entry {symbol} side={side} qty={qty} at {ltp:.2f}")
            _open_trade(symbol, side, qty, ltp)
            return

        oid = _place_live_order(kite, symbol, side, qty)
        if oid:
            append_log("INFO", "ORDER", f"LIVE entry {symbol} side={side} qty={qty} at {ltp:.2f} order_id={oid}")
            _open_trade(symbol, side, qty, ltp)
        else:
            append_log("ERROR", "ORDER", f"LIVE entry failed {symbol} side={side} qty={qty}")

    except SystemExit:
        raise
    except Exception as e:
        STATE["last_error"] = str(e)
        append_log("ERROR", "TICK", f"tick exception: {e}")

def run_loop_forever() -> None:
    """
    Called by bot.py using asyncio.to_thread(CYCLE.run_loop_forever)
    """
    tick_s = int(getattr(CFG, "TICK_SECONDS", 20))
    append_log("INFO", "ENGINE", f"Trading loop started (tick={tick_s}s, tz=Asia/Kolkata)")

    while True:
        try:
            tick()
        except SystemExit:
            raise
        except Exception as e:
            STATE["last_error"] = str(e)
            append_log("ERROR", "ENGINE", f"loop exception: {e}")

        time.sleep(tick_s)

# -----------------------------
# Simple control helpers (optional)
# -----------------------------
def pause() -> None:
    STATE["paused"] = True
    append_log("INFO", "ENGINE", "Paused=True")

def resume() -> None:
    STATE["paused"] = False
    append_log("INFO", "ENGINE", "Paused=False")

def set_today_pnl(value: float) -> None:
    STATE["today_pnl"] = float(value)

def set_open_trade(trade: Optional[Dict[str, Any]]) -> None:
    STATE["open_trade"] = trade
    STATE["peak_pct"] = None
