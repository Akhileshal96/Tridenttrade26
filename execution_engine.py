import config as CFG
from log_store import append_log


def check_time_exit(force_exit_check) -> bool:
    return bool(force_exit_check())


def check_stoploss(pnl_pct: float, side: str = "LONG", product: str = "MIS") -> bool:
    s = str(side or "LONG").upper()
    is_swing = str(product or "MIS").upper() == "CNC"
    if s == "SHORT":
        lim = abs(float(getattr(CFG, "SHORT_STOPLOSS_PCT", 1.2)))
    else:
        lim = abs(float(getattr(CFG, "STOPLOSS_PCT", 2.0)))
    # Swing/CNC trades get wider stoploss (1.5x) — overnight gaps and
    # multi-day holds need more room to breathe.
    if is_swing:
        swing_mult = float(getattr(CFG, "SWING_STOPLOSS_MULT", 1.5) or 1.5)
        lim = lim * swing_mult
    return pnl_pct <= -lim


def check_trailing(trade: dict, pnl_inr: float, trigger_inr: float) -> bool:
    trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
    return trail_active and pnl_inr <= trigger_inr


def place_live_order(place_order_fn, sym: str, side: str, qty: int):
    return place_order_fn(sym, side, qty)


def close_position(close_position_fn, sym: str, reason: str, ltp_override=None):
    return close_position_fn(sym, reason=reason, ltp_override=ltp_override)


def _calc_trail_activate_inr(entry: float, qty: int, tier: str = "FULL", side: str = "LONG", product: str = "MIS") -> float:
    position_value = float(entry) * max(1, int(qty))
    t = str(tier or "FULL").upper()
    s = str(side or "LONG").upper()
    if t == "MICRO":
        pct = float(getattr(CFG, "TRAIL_ACTIVATE_MICRO_PCT", 0.18) or 0.18) / 100.0
        floor = float(getattr(CFG, "TRAIL_ACTIVATE_MICRO_FLOOR_INR", 2.0) or 2.0)
    elif t == "REDUCED":
        pct = float(getattr(CFG, "TRAIL_ACTIVATE_REDUCED_PCT", 0.22) or 0.22) / 100.0
        floor = float(getattr(CFG, "TRAIL_ACTIVATE_REDUCED_FLOOR_INR", 3.0) or 3.0)
    else:
        pct = float(getattr(CFG, "TRAIL_ACTIVATE_FULL_PCT", 0.30) or 0.30) / 100.0
        floor = float(getattr(CFG, "TRAIL_ACTIVATE_FULL_FLOOR_INR", 5.0) or 5.0)
    if s == "SHORT" and position_value <= float(getattr(CFG, "SHORT_SMALL_POSITION_VALUE_INR", 8000.0) or 8000.0):
        floor = min(floor, float(getattr(CFG, "SHORT_SMALL_TRAIL_FLOOR_INR", 3.0) or 3.0))
    # Swing trades: higher activation threshold — let profits build before
    # engaging trail. Prevents premature trailing on normal intraday noise.
    if str(product or "MIS").upper() == "CNC":
        pct *= float(getattr(CFG, "SWING_TRAIL_ACTIVATE_MULT", 2.0) or 2.0)
        floor *= float(getattr(CFG, "SWING_TRAIL_ACTIVATE_MULT", 2.0) or 2.0)
    dynamic = position_value * pct
    return max(floor, dynamic)


def _dynamic_trail_levels(peak_pnl_inr: float, tier: str, product: str = "MIS") -> tuple[float, float]:
    t = str(tier or "FULL").upper()
    if t == "MICRO":
        be_arm = float(getattr(CFG, "TRAIL_BE_ARM_MICRO_INR", 2.5) or 2.5)
        min_lock_floor = float(getattr(CFG, "TRAIL_BE_LOCK_MICRO_INR", 0.1) or 0.1)
    elif t == "REDUCED":
        be_arm = float(getattr(CFG, "TRAIL_BE_ARM_REDUCED_INR", 4.0) or 4.0)
        min_lock_floor = float(getattr(CFG, "TRAIL_BE_LOCK_REDUCED_INR", 0.2) or 0.2)
    else:
        be_arm = float(getattr(CFG, "TRAIL_BE_ARM_FULL_INR", 6.0) or 6.0)
        min_lock_floor = float(getattr(CFG, "TRAIL_BE_LOCK_FULL_INR", 0.5) or 0.5)
    # Swing/CNC trades get wider giveback — multi-day holds have bigger
    # intraday swings, tighter trailing causes premature exits.
    is_swing = str(product or "MIS").upper() == "CNC"
    swing_widen = float(getattr(CFG, "SWING_TRAIL_WIDEN_MULT", 1.5) or 1.5) if is_swing else 1.0

    if peak_pnl_inr < be_arm:
        # Early profit stage: 35% giveback (intraday) / 52% (swing)
        return 0.0, max(2.0 * swing_widen, peak_pnl_inr * 0.35 * swing_widen)
    if peak_pnl_inr < (be_arm * 2.0):
        # Breakeven stage: 30% giveback (intraday) / 45% (swing)
        return min_lock_floor, max(2.0 * swing_widen, peak_pnl_inr * 0.30 * swing_widen)
    # Strong profit stage: lock 10%, 20% giveback (intraday) / 30% (swing)
    return max(min_lock_floor, peak_pnl_inr * 0.10), max(1.5 * swing_widen, peak_pnl_inr * 0.20 * swing_widen)


def force_exit_all(positions: dict, close_position_fn, reason="TIME"):
    ok = True
    for sym in list((positions or {}).keys()):
        ok = bool(close_position(close_position_fn, sym, reason=reason)) and ok
    return ok


def monitor_positions(state: dict, positions: dict, get_ltp, close_position, force_exit_check):
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
            # Track consecutive LTP failures per symbol. After 3 consecutive
            # failures, close position at last known price to avoid holding
            # a position we can no longer monitor.
            fail_key = f"_ltp_fail_{sym}"
            fails = int(trade.get(fail_key) or 0) + 1
            trade[fail_key] = fails
            append_log("WARN", "LTP", f"{sym} ltp_unavailable consecutive_fails={fails}")
            if fails >= 3:
                last_entry = float(trade.get("entry") or entry)
                append_log("ERROR", "LTP", f"{sym} ltp_unavailable for {fails} cycles → emergency close at entry price")
                close_position(sym, reason="LTP_UNAVAILABLE", ltp_override=last_entry)
            continue
        # Reset LTP failure counter on successful fetch
        trade.pop(f"_ltp_fail_{sym}", None)

        # Skip time-based exit for CNC (swing) trades — they hold overnight.
        trade_product = str(trade.get("product") or "MIS").upper()
        if trade_product != "CNC" and check_time_exit(force_exit_check):
            close_position(sym, reason="TIME", ltp_override=ltp)
            continue

        side = str(trade.get("side") or "LONG").upper()
        if side == "SHORT":
            pnl_pct = ((entry - ltp) / entry) * 100.0
            pnl_inr = (entry - ltp) * qty
        else:
            pnl_pct = ((ltp - entry) / entry) * 100.0
            pnl_inr = (ltp - entry) * qty
        position_value = entry * qty
        tier = str(trade.get("confidence_tier") or "FULL").upper()

        peak_pnl_inr = float(trade.get("peak_pnl_inr") or 0.0)
        peak_pnl_inr = max(peak_pnl_inr, pnl_inr)
        trade["peak_pnl_inr"] = peak_pnl_inr

        # Keep legacy peak% fields updated for compatibility/debug visibility.
        existing_peak_pct = float(trade.get("peak_pct") or trade.get("peak") or 0.0)
        peak_pct = max(existing_peak_pct, pnl_pct)
        trade["peak_pct"] = peak_pct
        trade["peak"] = peak_pct

        activate_inr = _calc_trail_activate_inr(entry, qty, tier=tier, side=side, product=trade_product)

        trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
        if (not trail_active) and pnl_inr >= activate_inr:
            trail_active = True
            trade["trail_active"] = True
            trade["trailing_active"] = True
            append_log("INFO", "TRAIL", f"{sym} activated pnl_inr={pnl_inr:.2f} activate_inr={activate_inr:.2f}")

        min_locked_pnl, allowed_giveback_inr = _dynamic_trail_levels(peak_pnl_inr, tier, product=trade_product)
        trigger_inr = max(min_locked_pnl, peak_pnl_inr - allowed_giveback_inr)
        if min_locked_pnl > 0:
            trade["breakeven_armed"] = True

        if check_stoploss(pnl_pct, side=side, product=trade_product):
            close_position(sym, reason="SL", ltp_override=ltp)
            continue

        append_log(
            "INFO",
            "RISK",
            f"[RISK] {sym} side={side} qty={qty} tier={tier} entry={entry:.2f} ltp={ltp:.2f} pnl_inr={pnl_inr:.2f} "
            f"peak_pnl_inr={peak_pnl_inr:.2f} trail_active={trail_active} activate_inr={activate_inr:.2f} "
            f"min_locked_pnl={min_locked_pnl:.2f} allowed_giveback_inr={allowed_giveback_inr:.2f} trigger_inr={trigger_inr:.2f}",
        )

        if check_trailing(trade, pnl_inr, trigger_inr):
            reason = "BREAKEVEN_LOCK" if (min_locked_pnl > 0 and pnl_inr <= min_locked_pnl) else "TRAIL"
            append_log(
                "WARN",
                "EXIT",
                f"{sym} reason={reason} pnl_inr={pnl_inr:.2f} peak_pnl_inr={peak_pnl_inr:.2f} "
                f"min_locked_pnl={min_locked_pnl:.2f} allowed_giveback_inr={allowed_giveback_inr:.2f} trigger_inr={trigger_inr:.2f}",
            )
            close_position(sym, reason=reason, ltp_override=ltp)


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
