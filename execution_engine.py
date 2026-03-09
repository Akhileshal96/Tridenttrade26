import config as CFG
from log_store import append_log


def check_time_exit(force_exit_check) -> bool:
    return bool(force_exit_check())


def check_stoploss(pnl_pct: float) -> bool:
    return pnl_pct <= -abs(float(getattr(CFG, "STOPLOSS_PCT", 2.0)))


def check_trailing(trade: dict, pnl_pct: float) -> bool:
    peak = float(trade.get("peak") or trade.get("peak_pct") or 0.0)
    trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
    trail = float(getattr(CFG, "TRAIL_PCT", 0.4))
    buf = float(getattr(CFG, "BUFFER_PCT", 0.05))
    return trail_active and pnl_pct <= (peak - trail - buf)


def place_live_order(place_order_fn, sym: str, side: str, qty: int):
    return place_order_fn(sym, side, qty)


def close_position(close_position_fn, sym: str, reason: str, ltp_override=None):
    return close_position_fn(sym, reason=reason, ltp_override=ltp_override)


def force_exit_all(positions: dict, close_position_fn, reason="TIME"):
    ok = True
    for sym in list((positions or {}).keys()):
        ok = bool(close_position(close_position_fn, sym, reason=reason)) and ok
    return ok


def monitor_positions(state: dict, positions: dict, get_ltp, close_position, force_exit_check):
    activate_pct = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 0.8))
    trail_pct = float(getattr(CFG, "TRAIL_PCT", 0.4))
    buf_pct = float(getattr(CFG, "BUFFER_PCT", 0.05))

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

        if check_time_exit(force_exit_check):
            close_position(sym, reason="TIME", ltp_override=ltp)
            continue

        pnl_pct = ((ltp - entry) / entry) * 100.0

        existing_peak = float(trade.get("peak_pct") or trade.get("peak") or 0.0)
        peak = max(existing_peak, pnl_pct)
        trade["peak"] = peak
        trade["peak_pct"] = peak

        trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
        if (not trail_active) and pnl_pct >= activate_pct:
            trail_active = True
            trade["trail_active"] = True
            trade["trailing_active"] = True
            append_log("INFO", "TRAIL", f"{sym} activated at pnl%={pnl_pct:.2f}")

        trigger_pct = peak - trail_pct - buf_pct

        if check_stoploss(pnl_pct):
            close_position(sym, reason="SL", ltp_override=ltp)
            continue

        append_log(
            "INFO",
            "RISK",
            f"{sym} pnl%={pnl_pct:.2f} peak%={peak:.2f} trail_active={trail_active} trigger<={trigger_pct:.2f}",
        )

        if check_trailing(trade, pnl_pct):
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
