from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config as CFG
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")


def monitor_positions(state: dict, positions: dict, get_ltp, close_position, force_exit_check):
    for sym, trade in list((positions or {}).items()):
        entry = float(trade.get("entry") or trade.get("entry_price") or 0.0)
        if entry <= 0:
            continue
        try:
            ltp = get_ltp(sym)
        except Exception:
            ltp = None
        if ltp is None:
            continue
        if force_exit_check():
            close_position(sym, reason="TIME", ltp_override=ltp)
            continue

        pnl_pct = ((ltp - entry) / entry) * 100.0
        peak = float(trade.get("peak") or trade.get("peak_pct") or pnl_pct)
        if pnl_pct > peak:
            peak = pnl_pct
        trade["peak"] = peak
        trade["peak_pct"] = peak

        if pnl_pct >= float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5)):
            trade["trail_active"] = True
            trade["trailing_active"] = True

        if pnl_pct <= -abs(float(getattr(CFG, "STOPLOSS_PCT", 2.0))):
            close_position(sym, reason="SL", ltp_override=ltp)
            continue

        trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
        trail = float(getattr(CFG, "TRAIL_PCT", 0.6))
        buf = float(getattr(CFG, "BUFFER_PCT", 0.1))
        append_log("INFO", "RISK", f"{sym} pnl%={pnl_pct:.2f} peak%={peak:.2f} trail_active={trail_active}")
        if trail_active and pnl_pct <= (peak - trail - buf):
            close_position(sym, reason="TRAIL", ltp_override=ltp)


def process_entries(universe, positions: dict, signal_fn, try_enter_fn, max_new=5):
    opened = 0
    blocked = set()
    while opened < max_new:
        held = set((positions or {}).keys())
        candidates = [s for s in universe if s not in held and s not in blocked]
        if not candidates:
            break
        for s in candidates:
            append_log("INFO", "SCAN", f"Scanning {s}")
        sig = signal_fn(candidates)
        if not sig:
            break
        if try_enter_fn(sig):
            opened += 1
        else:
            sym = str(sig.get("symbol") or "").strip().upper()
            if sym:
                blocked.add(sym)
