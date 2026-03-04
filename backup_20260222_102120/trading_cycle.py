import os
import time
from datetime import datetime

import config as CFG
from log_store import append_log
from strategy_engine import generate_signal
from broker_zerodha import get_kite


# ---------- Paths / Storage ----------
DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

EXCLUSIONS_FILE = os.path.join(DATA_DIR, "exclusions.txt")


# ---------- Runtime State ----------
STATE = {
    "paused": True,

    # Live safety gate:
    # LIVE only when initiated==True and (IS_LIVE==True OR live_override==True)
    "initiated": False,
    "live_override": False,

    # Trade/PnL:
    "open_trade": None,     # dict: {symbol, side, entry, qty, order_id?, sl_price?, peak?}
    "today_pnl": 0.0,
    "day_key": datetime.now().strftime("%Y-%m-%d"),
}

# Runtime params that can be changed via Telegram commands without redeploy
RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.30")),
}


# ---------- Helpers ----------
def _ensure_day_key():
    today = datetime.now().strftime("%Y-%m-%d")
    if STATE.get("day_key") != today:
        # New day rollover
        STATE["day_key"] = today
        STATE["today_pnl"] = 0.0
        STATE["open_trade"] = None
        append_log("INFO", "DAY", f"Auto rollover reset for {today}")


def is_live_enabled() -> bool:
    """
    LIVE is allowed only if /initiate has been issued (initiated=True)
    AND either env IS_LIVE true OR runtime override true.
    This prevents accidental live trading just because env is set.
    """
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))


def set_runtime_param(key: str, value):
    RUNTIME[key] = value


def manual_reset_day():
    STATE["today_pnl"] = 0.0
    STATE["open_trade"] = None
    STATE["day_key"] = datetime.now().strftime("%Y-%m-%d")
    append_log("INFO", "DAY", "Manual day reset executed")
    return True


# ---------- Exclusions (Insider Safety) ----------
def _load_exclusions_set() -> set[str]:
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])


def _save_exclusions_set(s: set[str]):
    with open(EXCLUSIONS_FILE, "w") as f:
        for sym in sorted(s):
            f.write(sym + "\n")


def list_exclusions() -> str:
    s = _load_exclusions_set()
    if not s:
        return "✅ Excluded symbols: (none)"
    return "⛔ Excluded symbols:\n" + "\n".join(sorted(s))


def exclude_symbol(sym: str) -> str:
    sym = sym.strip().upper()
    if not sym:
        return "Usage: /exclude SYMBOL"
    s = _load_exclusions_set()
    s.add(sym)
    _save_exclusions_set(s)
    append_log("WARN", "EXCL", f"Excluded {sym}")
    return f"⛔ {sym} excluded permanently. (/include {sym} to release)"


def include_symbol(sym: str) -> str:
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


# ---------- Universe ----------
def load_universe() -> list[str]:
    """
    Load universe from file and filter excluded symbols.
    """
    if not os.path.exists(CFG.UNIVERSE_PATH):
        return []
    with open(CFG.UNIVERSE_PATH, "r") as f:
        syms = [l.strip().upper() for l in f if l.strip()]
    excl = _load_exclusions_set()
    out = [s for s in syms if s not in excl]
    # Optional cap
    if getattr(CFG, "UNIVERSE_SIZE", None):
        out = out[: int(CFG.UNIVERSE_SIZE)]
    return out


# ---------- Status ----------
def get_status_text() -> str:
    _ensure_day_key()
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    paused = STATE.get("paused")
    open_trade = STATE.get("open_trade")
    pnl = float(STATE.get("today_pnl") or 0.0)
    uni_count = len(load_universe())

    return (
        f"📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {paused}\n"
        f"Universe: {uni_count} symbols\n"
        f"Today PnL: {pnl:.2f}\n"
        f"Open Trade: {open_trade}\n\n"
        f"Caps:\n"
        f"- Daily Loss Cap: {CFG.DAILY_LOSS_CAP_INR}\n"
        f"- Daily Profit Target: {CFG.DAILY_PROFIT_TARGET_INR}\n"
        f"- Stoploss %: {CFG.STOPLOSS_PCT}\n"
        f"- Risk/Trade %: {CFG.RISK_PER_TRADE_PCT}\n"
        f"- Tick Seconds: {CFG.TICK_SECONDS}\n"
        f"- Max Slippage %: {RUNTIME.get('MAX_ENTRY_SLIPPAGE_PCT')}\n"
    )


# ---------- Risk / Sizing ----------
def _calc_qty(price: float) -> int:
    """
    Risk-based sizing:
    risk_amt = capital * risk_pct
    per_share_risk = price * stoploss_pct
    qty = min(affordable_qty, risk_qty)
    """
    capital = float(CFG.CAPITAL_INR)
    risk_amt = capital * float(CFG.RISK_PER_TRADE_PCT) / 100.0
    per_share_risk = price * float(CFG.STOPLOSS_PCT) / 100.0

    if per_share_risk <= 0:
        return 1

    risk_qty = int(risk_amt / per_share_risk)
    affordable_qty = int(capital / price) if price > 0 else 0

    qty = max(1, min(risk_qty, affordable_qty))
    return qty


def _ltp(kite, sym: str):
    try:
        ins = f"{CFG.EXCHANGE}:{sym}"
        data = kite.ltp([ins])
        return float(data[ins]["last_price"])
    except Exception:
        return None


# ---------- Execution ----------
def _place_live_order(kite, sym: str, side: str, qty: int) -> str | None:
    """
    Places a market order in Zerodha (MIS).
    Returns order_id if successful.
    """
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


def _close_open_trade(reason: str = "MANUAL") -> bool:
    """
    Attempts to close current open trade.
    In PAPER: just clears state.
    In LIVE: places opposite market order.
    """
    trade = STATE.get("open_trade")
    if not trade:
        return False

    sym = trade.get("symbol")
    side = trade.get("side")
    qty = int(trade.get("qty") or 0) or 1

    # Opposite side to exit
    exit_side = "SELL" if side == "BUY" else "BUY"

    if not is_live_enabled():
        append_log("WARN", "EXIT", f"PAPER exit {sym} ({reason})")
        STATE["open_trade"] = None
        return True

    kite = get_kite()
    order_id = _place_live_order(kite, sym, exit_side, qty)
    if order_id:
        append_log("WARN", "EXIT", f"LIVE exit {sym} ({reason}) order_id={order_id}")
        STATE["open_trade"] = None
        return True

    return False


# ---------- Trading Loop ----------
def tick():
    _ensure_day_key()

    # Halt if paused
    if STATE.get("paused"):
        return

    # Daily caps safety
    if STATE["today_pnl"] <= -abs(CFG.DAILY_LOSS_CAP_INR):
        append_log("WARN", "CAP", "Daily loss cap hit. Pausing loop.")
        STATE["paused"] = True
        return

    if STATE["today_pnl"] >= abs(CFG.DAILY_PROFIT_TARGET_INR):
        append_log("INFO", "CAP", "Daily profit target hit. Pausing loop.")
        STATE["paused"] = True
        return

    # Force exit time (if you later add time parsing; keeping minimal for now)

    # Manage open trade (placeholder: you can extend with SL/Trail/Profit lock)
    if STATE.get("open_trade"):
        # You can implement SL/TP checks here using LTP.
        return

    universe = load_universe()
    if not universe:
        append_log("WARN", "UNIV", "Universe empty. Run /nightnow.")
        return

    sig = generate_signal(universe)
    if not sig:
        return

    sym = sig["symbol"]
    entry = float(sig.get("entry") or 0.0)

    qty = _calc_qty(entry if entry > 0 else 1.0)
    sl_price = entry * (1.0 - float(CFG.STOPLOSS_PCT) / 100.0)

    # Live slippage check
    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price:
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            # For BUY, reject if current price too far above signal entry
            if now_price > entry * (1.0 + max_slip):
                append_log("WARN", "SLIP", f"Skip {sym}: slip too high now={now_price} sig={entry}")
                return

        order_id = _place_live_order(kite, sym, "BUY", qty)
        if not order_id:
            return

        STATE["open_trade"] = {
            "symbol": sym,
            "side": "BUY",
            "entry": entry,
            "qty": qty,
            "order_id": order_id,
            "sl_price": sl_price,
            "peak": entry,
        }
        append_log("INFO", "TRADE", f"LIVE Entered {sym} qty={qty} entry={entry} sl={sl_price} oid={order_id}")
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
