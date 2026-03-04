# Trident Trading Cycle (Clean All-in-one v1)
# Includes:
# 1) Kite position -> STATE sync (live open trade visible in /status)
# 2) LIVE mode detection via /initiate override
# 3) Stable tick loop + timezone-correct time
# 4) Rich /status text

import os
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log

try:
    from broker_zerodha import get_kite
except Exception:
    get_kite = None

try:
    # strategy_engine should expose generate_signal(universe: list[str]) -> dict|None
    from strategy_engine import generate_signal
except Exception:
    generate_signal = None


IST = ZoneInfo("Asia/Kolkata")

# ---------------------------
# State
# ---------------------------
STATE = {
    "paused": True,           # loop paused until /startloop
    "initiated": False,       # set True when /initiate called (runtime override)
    "live_override": False,   # True when /initiate called (even if CFG.IS_LIVE is false)
    "engine_running": False,
    "open_trade": None,       # dict: symbol, side, qty, price, entry_ts, order_id(optional)
    "today_pnl": 0.0,
    "day_key": None,
    "peak": None,             # peak PnL% since entry (profit lock tracking)
    "last_tick_ts": None,
    "last_error": None,
}

RUNTIME = {
    # Can be updated by bot commands if needed
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "1.0")),
}

# ---------------------------
# Time helpers
# ---------------------------
def now() -> datetime:
    return datetime.now(IST)

def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _parse_hhmm(val: str) -> tuple[int, int]:
    h, m = val.strip().split(":")
    return int(h), int(m)

def _within_entry_window() -> bool:
    n = now()
    sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
    eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = n.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = n.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= n <= end

def _force_exit_time_reached() -> bool:
    # default 15:10
    n = now()
    fh, fm = _parse_hhmm(getattr(CFG, "FORCE_EXIT_AT", "15:10"))
    fx = n.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return n >= fx

def _ensure_day_key():
    today = now().strftime("%Y-%m-%d")
    if STATE.get("day_key") != today:
        STATE["day_key"] = today
        STATE["today_pnl"] = 0.0
        STATE["peak"] = None
        append_log("INFO", "DAY", f"New day {today} (reset today_pnl/peak)")

# ---------------------------
# Mode / Live detection
# ---------------------------
def is_live_enabled() -> bool:
    """
    LIVE is enabled if:
    - CFG.IS_LIVE is True (config-based)
    OR
    - runtime override via /initiate was used (STATE['initiated'] / STATE['live_override'])
    """
    try:
        cfg_live = bool(getattr(CFG, "IS_LIVE", False))
    except Exception:
        cfg_live = False
    return cfg_live or bool(STATE.get("initiated")) or bool(STATE.get("live_override"))

def set_live_override(enabled: bool):
    STATE["live_override"] = bool(enabled)
    STATE["initiated"] = bool(enabled)
    append_log("INFO", "MODE", f"Live override set to {STATE['live_override']}")

# ---------------------------
# Broker helpers
# ---------------------------
def _safe_get_kite():
    if get_kite is None:
        return None
    try:
        return get_kite()
    except Exception as e:
        append_log("ERROR", "KITE", f"get_kite failed: {e}")
        return None

def _safe_ltp(kite, sym: str):
    try:
        d = kite.ltp([f"NSE:{sym}"])
        return float(d[f"NSE:{sym}"]["last_price"])
    except Exception as e:
        append_log("ERROR", "LTP", f"LTP failed {sym}: {e}")
        return None

# ---------------------------
# Sync: Kite position -> STATE
# ---------------------------
def sync_open_trade_from_kite():
    """
    If bot restarted and STATE lost, but Kite has an open position,
    rebuild STATE['open_trade'] from positions() so /status reflects reality.
    """
    if not is_live_enabled():
        return

    if STATE.get("open_trade"):
        return

    kite = _safe_get_kite()
    if not kite:
        return

    try:
        pos = kite.positions()
        net = pos.get("net") or []
        for p in net:
            # Focus: NSE MIS positions only (most common for your bot)
            if (p.get("exchange") == "NSE" and p.get("product") == getattr(CFG, "PRODUCT", "MIS")):
                qty = int(p.get("quantity") or 0)
                if qty != 0:
                    sym = p.get("tradingsymbol") or p.get("symbol") or ""
                    avg = float(p.get("average_price") or p.get("buy_price") or p.get("sell_price") or 0.0)
                    side = "BUY" if qty > 0 else "SELL"
                    STATE["open_trade"] = {
                        "symbol": sym,
                        "side": side,
                        "qty": abs(qty),
                        "price": avg if avg > 0 else None,
                        "entry_ts": now().isoformat(),
                        "synced": True,
                    }
                    STATE["peak"] = None
                    append_log("WARN", "SYNC", f"Synced open trade from Kite: {sym} side={side} qty={abs(qty)} avg={avg}")
                    return
    except Exception as e:
        append_log("ERROR", "SYNC", f"sync_open_trade_from_kite failed: {e}")

# ---------------------------
# Exit mechanism
# ---------------------------
def _place_live_exit_order(kite, sym: str, side: str, qty: int):
    # Market exit
    try:
        oid = kite.place_order(
            tradingsymbol=sym,
            exchange=getattr(CFG, "EXCHANGE", "NSE"),
            transaction_type=side,
            quantity=qty,
            order_type="MARKET",
            product=getattr(CFG, "PRODUCT", "MIS"),
            variety="regular",
        )
        return oid
    except Exception as e:
        append_log("ERROR", "EXIT", f"place_order failed: {e}")
        return None

def exit_trade(reason: str):
    trade = STATE.get("open_trade")
    if not trade:
        return False

    sym = trade.get("symbol")
    side = trade.get("side")
    qty = int(trade.get("qty") or 0) or 1

    # If you are long (BUY), exit is SELL; if short (SELL), exit is BUY
    exit_side = "SELL" if side == "BUY" else "BUY"

    if not is_live_enabled():
        append_log("WARN", "EXIT", f"PAPER exit {sym} ({reason})")
        STATE["open_trade"] = None
        STATE["peak"] = None
        return True

    kite = _safe_get_kite()
    if not kite:
        append_log("ERROR", "EXIT", "No kite instance available for live exit")
        return False

    oid = _place_live_exit_order(kite, sym, exit_side, qty)
    if oid:
        append_log("WARN", "EXIT", f"LIVE exit {sym} ({reason}) order_id={oid}")
        STATE["open_trade"] = None
        STATE["peak"] = None
        return True

    return False

# ---------------------------
# Profit lock (trail from peak)
# ---------------------------
def check_profit_lock(ltp: float):
    trade = STATE.get("open_trade")
    if not trade:
        return

    entry = trade.get("price")
    if not entry:
        return

    try:
        entry = float(entry)
        pnl_pct = ((ltp - entry) / entry) * 100.0 if trade.get("side") == "BUY" else ((entry - ltp) / entry) * 100.0
    except Exception:
        return

    if STATE.get("peak") is None:
        STATE["peak"] = pnl_pct

    STATE["peak"] = max(float(STATE["peak"]), pnl_pct)

    activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
    trail = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))

    if float(STATE["peak"]) >= activate:
        threshold = float(STATE["peak"]) - trail
        if pnl_pct < threshold:
            append_log("WARN", "LOCK", f"Profit lock hit: peak={STATE['peak']:.2f}% pnl={pnl_pct:.2f}% thr={threshold:.2f}%")
            exit_trade("profit_lock")

# ---------------------------
# Status
# ---------------------------
def get_status_text() -> str:
    n = now()
    mode = "LIVE" if is_live_enabled() else "PAPER"
    eng = "RUNNING" if STATE.get("engine_running") else "STOPPED"
    paused = bool(STATE.get("paused"))

    tick_s = int(getattr(CFG, "TICK_SECONDS", 20))
    ew_s = getattr(CFG, "ENTRY_START", "09:20")
    ew_e = getattr(CFG, "ENTRY_END", "14:30")

    lines = []
    lines.append(f"🕒 Time: {_fmt_dt(n)}")
    lines.append(f"⚙️ Mode: {mode} | Engine: {eng} | Paused: {paused}")
    lines.append(f"⏱ Tick: {tick_s}s | Entry window: {ew_s}–{ew_e}")
    lines.append(f"📈 Today PnL (bot): {STATE.get('today_pnl', 0.0):.2f} INR")

    trade = STATE.get("open_trade")
    if not trade:
        lines.append("🟦 Open trade: None")
    else:
        sym = trade.get("symbol")
        side = trade.get("side")
        qty = trade.get("qty")
        entry = trade.get("price")
        entry_ts = trade.get("entry_ts") or "NA"

        ltp_line = "LTP: NA"
        pnl_line = "PnL%: NA"

        kite = _safe_get_kite() if is_live_enabled() else None
        if kite and sym:
            ltp = _safe_ltp(kite, sym)
            if ltp is not None and entry:
                try:
                    entryf = float(entry)
                    ltp_line = f"LTP: {ltp:.2f}"
                    pnl_pct = ((ltp - entryf) / entryf) * 100.0 if side == "BUY" else ((entryf - ltp) / entryf) * 100.0
                    pnl_line = f"PnL%: {pnl_pct:.2f}%"
                except Exception:
                    pass

        lines.append(f"📊 Open trade: {sym} | side={side} | qty={qty}")
        if entry:
            lines.append(f"🧾 Entry: {entry} | {ltp_line} | {pnl_line}")
        else:
            lines.append(f"🧾 Entry: NA | {ltp_line} | {pnl_line}")

        lines.append(f"🧭 Entry time: {entry_ts}")

        activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
        trail = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))
        peak = STATE.get("peak")
        peak_s = "NA" if peak is None else f"{float(peak):.2f}%"
        lines.append(f"🔒 Profit lock: peak={peak_s} | activates at {activate:.2f}% | trail {trail:.2f}%")

    if STATE.get("last_error"):
        lines.append(f"❗ Last error: {STATE['last_error']}")

    return "\n".join(lines)

# ---------------------------
# Loop control
# ---------------------------
def start_loop():
    STATE["paused"] = False
    append_log("INFO", "LOOP", "Loop Started")
    return True

def stop_loop():
    STATE["paused"] = True
    append_log("INFO", "LOOP", "Loop Paused")
    return True

def initiate():
    # Used by /initiate
    set_live_override(True)
    append_log("INFO", "MODE", "LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
    return True

def disengage():
    set_live_override(False)
    append_log("WARN", "MODE", "LIVE DISENGAGED (back to config mode).")
    return True

def set_runtime_param(key: str, value):
    RUNTIME[key] = value
    append_log("INFO", "RUNTIME", f"{key}={value}")
    return True

# ---------------------------
# Tick
# ---------------------------
def tick():
    STATE["last_tick_ts"] = now().isoformat()
    _ensure_day_key()

    if STATE.get("paused"):
        return

    # Sync from Kite if needed
    sync_open_trade_from_kite()

    # Manage open trade exits
    trade = STATE.get("open_trade")
    if trade and is_live_enabled():
        kite = _safe_get_kite()
        if kite:
            sym = trade.get("symbol")
            if sym:
                ltp = _safe_ltp(kite, sym)
                if ltp is not None:
                    # Profit lock
                    check_profit_lock(ltp)

                    # Force exit time
                    if _force_exit_time_reached() and STATE.get("open_trade"):
                        exit_trade("force_exit_time")

    # Entry logic (keep minimal + safe)
    if not STATE.get("open_trade"):
        if not _within_entry_window():
            return
        if not is_live_enabled() and bool(getattr(CFG, "REQUIRE_LIVE_FOR_ENTRY", False)):
            return

        # If strategy engine exists, it can decide what to trade
        if generate_signal is None:
            return

        universe = []
        try:
            # If your CFG provides universe file, load it
            uni_file = getattr(CFG, "UNIVERSE_FILE", os.path.join(ROOT, "data", "universe.txt"))
        except Exception:
            uni_file = os.path.join(os.getcwd(), "data", "universe.txt")

        try:
            if os.path.exists(uni_file):
                with open(uni_file, "r", encoding="utf-8") as f:
                    universe = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        except Exception as e:
            append_log("ERROR", "SCAN", f"Universe file read failed: {e}")
            universe = []

        # Prevent NoneType iterable crash
        if universe is None:
            universe = []

        if not universe:
            # Don't crash. Just log occasionally.
            append_log("WARN", "SCAN", "Universe empty - skipping signal generation")
            return

        try:
            append_log("INFO", "SCAN", f"Scanning {len(universe)} symbols")
            sig = generate_signal(universe)
        except Exception as e:
            append_log("ERROR", "SIG", f"generate_signal failed: {e}")
            return

        if not sig:
            return

        # Expected keys: symbol, side, qty(optional)
        sym = sig.get("symbol")
        side = (sig.get("side") or "").upper()
        qty = int(sig.get("qty") or 1)

        if not sym or side not in ("BUY", "SELL"):
            return

        if not is_live_enabled():
            # Paper entry
            STATE["open_trade"] = {
                "symbol": sym,
                "side": side,
                "qty": qty,
                "price": float(sig.get("price") or 0.0) or None,
                "entry_ts": now().isoformat(),
                "paper": True,
            }
            STATE["peak"] = None
            append_log("INFO", "ORDER", f"PAPER entry {sym} side={side} qty={qty}")
            return

        # Live entry
        kite = _safe_get_kite()
        if not kite:
            return

        try:
            oid = kite.place_order(
                tradingsymbol=sym,
                exchange=getattr(CFG, "EXCHANGE", "NSE"),
                transaction_type=side,
                quantity=qty,
                order_type="MARKET",
                product=getattr(CFG, "PRODUCT", "MIS"),
                variety="regular",
            )
            # Fetch ltp as entry approx
            ltp = _safe_ltp(kite, sym)
            STATE["open_trade"] = {
                "symbol": sym,
                "side": side,
                "qty": qty,
                "price": ltp,
                "entry_ts": now().isoformat(),
                "order_id": oid,
            }
            STATE["peak"] = None
            append_log("INFO", "ORDER", f"LIVE entry {sym} side={side} qty={qty} order_id={oid}")
        except Exception as e:
            append_log("ERROR", "ORDER", f"Live entry failed: {e}")

# ---------------------------
# Engine runner
# ---------------------------
def run_loop_forever():
    STATE["engine_running"] = True
    tick_s = int(getattr(CFG, "TICK_SECONDS", 20))
    append_log("INFO", "ENGINE", f"Trading loop started (tick={tick_s}s, tz=Asia/Kolkata)")
    try:
        while True:
            try:
                tick()
            except Exception as e:
                STATE["last_error"] = str(e)
                append_log("ERROR", "TICK", f"tick exception: {e}")
                # avoid tight crash loop
                time.sleep(1)
            time.sleep(tick_s)
    finally:
        STATE["engine_running"] = False
        append_log("WARN", "ENGINE", "Trading loop stopped")
