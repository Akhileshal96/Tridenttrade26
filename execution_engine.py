import config as CFG
from log_store import append_log


def check_time_exit(force_exit_check) -> bool:
    return bool(force_exit_check())


def check_stoploss(pnl_pct: float) -> bool:
    return pnl_pct <= -abs(float(getattr(CFG, "STOPLOSS_PCT", 2.0)))


def check_trailing(trade: dict, pnl_inr: float, trigger_inr: float) -> bool:
    trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
    return trail_active and pnl_inr <= trigger_inr


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
    min_activate_inr = float(getattr(CFG, "MIN_TRAIL_ACTIVATE_INR", 8.0))
    activate_pct_of_position = float(getattr(CFG, "TRAIL_ACTIVATE_PCT_OF_POSITION", 0.4))
    trail_lock_ratio = float(getattr(CFG, "TRAIL_LOCK_RATIO", 0.5))
    trail_buffer_inr = float(getattr(CFG, "TRAIL_BUFFER_INR", 1.0))

    for sym, trade in list((positions or {}).items()):
        entry = float(trade.get("entry") or trade.get("entry_price") or 0.0)
        qty = int(trade.get("qty") or trade.get("quantity") or 1)
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
        pnl_inr = (ltp - entry) * qty
        position_value = entry * qty

        peak_pnl_inr = float(trade.get("peak_pnl_inr") or 0.0)
        peak_pnl_inr = max(peak_pnl_inr, pnl_inr)
        trade["peak_pnl_inr"] = peak_pnl_inr

        # Keep legacy peak% fields updated for compatibility/debug visibility.
        existing_peak_pct = float(trade.get("peak_pct") or trade.get("peak") or 0.0)
        peak_pct = max(existing_peak_pct, pnl_pct)
        trade["peak_pct"] = peak_pct
        trade["peak"] = peak_pct

        activate_inr = max(min_activate_inr, position_value * activate_pct_of_position / 100.0)

        trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
        if (not trail_active) and pnl_inr >= activate_inr:
            trail_active = True
            trade["trail_active"] = True
            trade["trailing_active"] = True
            append_log("INFO", "TRAIL", f"{sym} activated pnl_inr={pnl_inr:.2f} activate_inr={activate_inr:.2f}")

        trigger_inr = (peak_pnl_inr * trail_lock_ratio) - trail_buffer_inr

        if check_stoploss(pnl_pct):
            close_position(sym, reason="SL", ltp_override=ltp)
            continue

        append_log(
            "INFO",
            "RISK",
            f"{sym} qty={qty} entry={entry:.2f} ltp={ltp:.2f} pnl_inr={pnl_inr:.2f} "
            f"peak_pnl_inr={peak_pnl_inr:.2f} trail_active={trail_active} "
            f"activate_inr={activate_inr:.2f} trigger_inr={trigger_inr:.2f}",
        )

        if check_trailing(trade, pnl_inr, trigger_inr):
            append_log(
                "WARN",
                "EXIT",
                f"{sym} reason=TRAIL pnl_inr={pnl_inr:.2f} peak_pnl_inr={peak_pnl_inr:.2f} trigger_inr={trigger_inr:.2f}",
            )
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
