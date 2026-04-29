from datetime import datetime
import time
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")


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

    # High-profit stage: once peak crosses threshold (default ₹100), lock 90%
    # and only allow 10% giveback — keeps nearly all of a big winner.
    high_profit_inr = float(getattr(CFG, "TRAIL_HIGH_PROFIT_INR", 100.0) or 100.0)
    if peak_pnl_inr >= high_profit_inr:
        lock_pct = float(getattr(CFG, "TRAIL_HIGH_PROFIT_LOCK_PCT", 0.90) or 0.90)
        give_pct = float(getattr(CFG, "TRAIL_HIGH_PROFIT_GIVEBACK_PCT", 0.10) or 0.10)
        return peak_pnl_inr * lock_pct, peak_pnl_inr * give_pct

    if peak_pnl_inr < be_arm:
        # Early profit stage: 35% giveback (intraday) / 52% (swing)
        return 0.0, max(2.0 * swing_widen, peak_pnl_inr * 0.35 * swing_widen)
    if peak_pnl_inr < (be_arm * 2.0):
        # Breakeven stage: 30% giveback (intraday) / 45% (swing)
        return min_lock_floor, max(2.0 * swing_widen, peak_pnl_inr * 0.30 * swing_widen)
    # Strong profit stage: lock 10%, 15% giveback (intraday) / 22% (swing)
    give_pct = float(getattr(CFG, "TRAIL_STRONG_GIVEBACK_PCT", 0.15))
    return max(min_lock_floor, peak_pnl_inr * 0.10), max(1.5 * swing_widen, peak_pnl_inr * give_pct * swing_widen)


def _fetch_ohlc_peak_pnl(sym: str, entry: float, qty: int, side: str, entry_time_str: str) -> float:
    try:
        import pandas as pd
        from broker_zerodha import get_kite
        from instrument_store import token_for_symbol
        token = token_for_symbol(sym)
        if not token:
            return 0.0
        entry_dt = datetime.fromisoformat(entry_time_str)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=IST)
        data = get_kite().historical_data(token, entry_dt, datetime.now(IST), "minute")
        if not data:
            return 0.0
        df = pd.DataFrame(data)
        if df.empty:
            return 0.0
        if str(side).upper() == "SHORT":
            return max(0.0, (entry - float(df["low"].min())) * qty)
        return max(0.0, (float(df["high"].max()) - entry) * qty)
    except Exception as e:
        append_log("WARN", "OHLC_PEAK", f"{sym} fetch failed: {e}")
        return 0.0


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
        entry_time_str = str(trade.get("entry_time") or "")

        peak_pnl_inr = float(trade.get("peak_pnl_inr") or 0.0)
        peak_pnl_inr = max(peak_pnl_inr, pnl_inr)
        trade["peak_pnl_inr"] = peak_pnl_inr

        # Keep legacy peak% fields updated for compatibility/debug visibility.
        existing_peak_pct = float(trade.get("peak_pct") or trade.get("peak") or 0.0)
        peak_pct = max(existing_peak_pct, pnl_pct)
        trade["peak_pct"] = peak_pct
        trade["peak"] = peak_pct

        # --- OHLC peak tracking: capture intra-tick spikes the 20s sampler misses ---
        if bool(getattr(CFG, "USE_OHLC_PEAK_TRACKING", True)) and entry_time_str:
            ohlc_refresh_sec = float(getattr(CFG, "OHLC_PEAK_REFRESH_SEC", 60))
            last_ohlc = float(trade.get("_last_ohlc_ts") or 0.0)
            if (time.monotonic() - last_ohlc) >= ohlc_refresh_sec:
                ohlc_peak = _fetch_ohlc_peak_pnl(sym, entry, qty, side, entry_time_str)
                trade["_last_ohlc_ts"] = time.monotonic()
                if ohlc_peak > peak_pnl_inr:
                    append_log("INFO", "OHLC_PEAK", f"{sym} ohlc_peak={ohlc_peak:.2f} ltp_peak={peak_pnl_inr:.2f} delta={ohlc_peak - peak_pnl_inr:.2f}")
                    peak_pnl_inr = ohlc_peak
                    trade["peak_pnl_inr"] = peak_pnl_inr

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

        # --- ATR-based adaptive stoploss (per-stock volatility) ---
        entry_atr = float(trade.get("entry_atr") or 0.0)
        sl_used = "FIXED"
        if entry_atr > 0 and bool(getattr(CFG, "USE_ATR_STOPLOSS", True)):
            atr_mult = float(getattr(CFG, "ATR_STOPLOSS_MULT", 1.5))
            if side == "SHORT":
                atr_mult = float(getattr(CFG, "ATR_STOPLOSS_SHORT_MULT", 1.2))
            is_swing = trade_product == "CNC"
            if is_swing:
                atr_mult *= float(getattr(CFG, "SWING_STOPLOSS_MULT", 1.5) or 1.5)
            atr_stop_dist = entry_atr * atr_mult
            # Hard ceiling: never wider than ATR_STOPLOSS_MAX_PCT
            max_pct = float(getattr(CFG, "ATR_STOPLOSS_MAX_PCT", 4.0))
            atr_stop_pct = (atr_stop_dist / entry) * 100.0 if entry > 0 else 0.0
            if atr_stop_pct > max_pct:
                atr_stop_dist = entry * max_pct / 100.0
                atr_stop_pct = max_pct
            if side == "SHORT":
                atr_sl_hit = ltp >= entry + atr_stop_dist
            else:
                atr_sl_hit = ltp <= entry - atr_stop_dist
            if atr_sl_hit:
                append_log(
                    "WARN", "SL",
                    f"{sym} ATR stoploss hit atr={entry_atr:.2f} mult={atr_mult:.2f} "
                    f"stop_dist={atr_stop_dist:.2f} stop_pct={atr_stop_pct:.1f}%",
                )
                close_position(sym, reason="SL_ATR", ltp_override=ltp)
                continue
            sl_used = f"ATR({atr_stop_pct:.1f}%)"
        # Fixed SL is always checked as a hard floor — even when ATR is active and
        # hasn't triggered yet. Prevents ATR's 4% ceiling from overriding the 2% cap.
        if check_stoploss(pnl_pct, side=side, product=trade_product):
            close_position(sym, reason="SL", ltp_override=ltp)
            continue

        # --- Per-trade profit target (2R hard exit) ---
        if bool(getattr(CFG, "USE_PROFIT_TARGET", True)):
            r_mult = float(getattr(CFG, "PROFIT_TARGET_R", 2.0))
            if entry_atr > 0 and bool(getattr(CFG, "USE_ATR_STOPLOSS", True)):
                sl_mult = float(getattr(CFG, "ATR_STOPLOSS_SHORT_MULT" if side == "SHORT" else "ATR_STOPLOSS_MULT", 1.5))
                r_dist = entry_atr * sl_mult
            else:
                sl_pct = float(getattr(CFG, "SHORT_STOPLOSS_PCT" if side == "SHORT" else "STOPLOSS_PCT", 2.0))
                r_dist = entry * sl_pct / 100.0
            target_dist = r_dist * r_mult
            target_hit = (ltp <= entry - target_dist) if side == "SHORT" else (ltp >= entry + target_dist)
            if target_hit:
                target_pct = (target_dist / entry) * 100.0 if entry > 0 else 0.0
                append_log("WARN", "EXIT", f"{sym} PROFIT_TARGET r_mult={r_mult:.1f}x target_pct={target_pct:.1f}% pnl_inr={pnl_inr:.2f}")
                close_position(sym, reason="PROFIT_TARGET", ltp_override=ltp)
                continue

        append_log(
            "INFO",
            "RISK",
            f"[RISK] {sym} side={side} qty={qty} tier={tier} entry={entry:.2f} ltp={ltp:.2f} pnl_inr={pnl_inr:.2f} "
            f"peak_pnl_inr={peak_pnl_inr:.2f} trail_active={trail_active} activate_inr={activate_inr:.2f} "
            f"min_locked_pnl={min_locked_pnl:.2f} allowed_giveback_inr={allowed_giveback_inr:.2f} trigger_inr={trigger_inr:.2f} "
            f"sl_type={sl_used}",
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
            continue

        # --- Failed development exit: close non-developing positions early ---
        if (
            entry_time_str
            and trade_product != "CNC"
            and not trail_active
            and bool(getattr(CFG, "USE_FAILED_DEV_EXIT", True))
        ):
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                elapsed_min = (datetime.now(IST) - entry_dt).total_seconds() / 60.0
                failed_dev_min = float(getattr(CFG, "FAILED_DEV_MINUTES", 30))
                failed_dev_ratio = float(getattr(CFG, "FAILED_DEV_PEAK_RATIO", 0.25))
                if elapsed_min >= failed_dev_min and peak_pnl_inr < activate_inr * failed_dev_ratio:
                    append_log(
                        "WARN", "EXIT",
                        f"{sym} FAILED_DEV exit elapsed={elapsed_min:.0f}min "
                        f"peak_pnl_inr={peak_pnl_inr:.2f} threshold={activate_inr * failed_dev_ratio:.2f} "
                        f"(activate_inr={activate_inr:.2f} ratio={failed_dev_ratio})",
                    )
                    close_position(sym, reason="FAILED_DEV", ltp_override=ltp)
                    continue
            except Exception:
                pass

        # --- Time-decay exit: close flat positions held too long (MIS only) ---
        if (
            entry_time_str
            and trade_product != "CNC"
            and not trail_active
            and bool(getattr(CFG, "USE_TIME_DECAY_EXIT", True))
        ):
            try:
                entry_dt = datetime.fromisoformat(entry_time_str)
                elapsed_min = (datetime.now(IST) - entry_dt).total_seconds() / 60.0
                decay_min = float(getattr(CFG, "TIME_DECAY_MINUTES", 90))
                decay_max_pnl = float(getattr(CFG, "TIME_DECAY_MAX_PNL_PCT", 0.3))
                # Also exit slow bleeders: positions held too long at a small loss
                # (above the bleed floor) that haven't hit the SL are dead weight.
                decay_bleed_floor = float(getattr(CFG, "TIME_DECAY_BLEED_FLOOR_PCT", -1.5))
                if elapsed_min >= decay_min and decay_bleed_floor <= pnl_pct <= decay_max_pnl:
                    append_log(
                        "WARN", "EXIT",
                        f"{sym} TIME_DECAY exit elapsed={elapsed_min:.0f}min pnl_pct={pnl_pct:.2f}% "
                        f"(held>{decay_min:.0f}min range=[{decay_bleed_floor}%,{decay_max_pnl}%])",
                    )
                    close_position(sym, reason="TIME_DECAY", ltp_override=ltp)
            except Exception:
                pass


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
