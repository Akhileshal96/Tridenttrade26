import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")


def get_bucket_from_slab(wallet: float):
    """Aggressive percentage-based bucket sizing for concentrated trades.

    Allocates 35% of wallet per trade, clamped between 15-50% of wallet.
    Fewer, bigger positions = more profit per winning trade.
    """
    bucket_pct = float(getattr(CFG, "BUCKET_ALLOC_PCT", 35.0) or 35.0)
    bucket = wallet * max(1.0, bucket_pct) / 100.0
    floor_pct = float(getattr(CFG, "BUCKET_FLOOR_PCT", 15.0) or 15.0)
    ceil_pct = float(getattr(CFG, "BUCKET_CEIL_PCT", 50.0) or 50.0)
    bmin = wallet * max(1.0, floor_pct) / 100.0
    bmax = wallet * max(floor_pct + 1, ceil_pct) / 100.0
    return max(bmin, min(bucket, bmax))


def get_position_size(price: float, wallet: float, bucket: float | None = None):
    bucket_val = float(bucket if bucket is not None else get_bucket_from_slab(wallet))
    bucket_qty = int(bucket_val / price) if price > 0 else 0
    risk_amt = wallet * float(getattr(CFG, "RISK_PER_TRADE_PCT", 2.0)) / 100.0
    per_share_risk = price * float(getattr(CFG, "STOPLOSS_PCT", 2.0)) / 100.0
    risk_qty = int(risk_amt / per_share_risk) if per_share_risk > 0 else 0
    # Blend: risk acts as drag not hard cap (70/30 weighted average)
    if bucket_qty > risk_qty and risk_qty > 0:
        qty = int(risk_qty * 0.30 + bucket_qty * 0.70)
    else:
        qty = min(bucket_qty, risk_qty)
    return max(0, qty), bucket_val, bucket_qty, risk_qty


def get_current_exposure(positions: dict):
    total = 0.0
    for t in (positions or {}).values():
        e = float(t.get("entry") or t.get("entry_price") or 0.0)
        q = int(t.get("qty") or t.get("quantity") or 0)
        total += e * q
    return total


def check_sector_exposure(symbol: str, positions: dict, sector: str | None = None):
    limit = int(os.getenv("MAX_POSITIONS_PER_SECTOR", "3"))
    if limit <= 0:
        return True
    target_sector = (sector or "UNKNOWN").strip().upper()
    if not target_sector or target_sector == "UNKNOWN":
        return True
    in_sector = 0
    for p in (positions or {}).values():
        p_sector = str(p.get("sector") or "UNKNOWN").strip().upper()
        if p_sector == target_sector:
            in_sector += 1
    if in_sector >= limit:
        append_log("INFO", "SKIP", f"{symbol} reason=sector_limit sector={target_sector} held={in_sector} limit={limit}")
        return False
    return True


def can_enter_trade(symbol: str, price: float, positions: dict, wallet: float, qty: int, *,
                    sector: str | None = None, max_exposure_pct: float | None = None):
    required = float(price) * max(1, int(qty or 1))
    current = get_current_exposure(positions)
    # Caller may pass the effective (GOD-aware) exposure pct directly.
    if max_exposure_pct is None:
        max_exposure_pct = float(getattr(CFG, "MAX_EXPOSURE_PCT", 75.0))
    max_exp = wallet * max_exposure_pct / 100.0
    if current + required > max_exp:
        append_log("INFO", "SKIP", f"{symbol} reason=exposure next={current+required:.2f} max={max_exp:.2f}")
        return False
    return check_sector_exposure(symbol, positions, sector=sector)


def update_loss_streak(state: dict, result: float, risk_profile: str = "STANDARD"):
    streak = int(state.get("loss_streak") or 0)
    wins = int(state.get("consecutive_wins") or 0)
    god = str(risk_profile or "STANDARD").upper() == "GOD"
    if result < 0:
        streak += 1
        wins = 0
    elif result > 0:
        streak = 0
        wins += 1
    # result == 0 (scratch/break-even): neither extends streak nor counts as recovery win
    state["loss_streak"] = streak
    state["consecutive_wins"] = wins

    halt_threshold = int(getattr(CFG, "LOSS_STREAK_HALT_THRESHOLD", 4) or 4)
    if streak >= halt_threshold:
        # Hard halt applies even in GOD — consecutive losses circuit breaker.
        state["halt_for_day"] = True
        append_log("WARN", "RISK", f"loss_streak={streak} (>={halt_threshold}) → stopping new trades for the day")
    elif streak >= 3:
        if not god:
            state["pause_entries_until"] = datetime.now(IST) + timedelta(minutes=30)
            state["reduce_size_factor"] = 0.5
            append_log("WARN", "RISK", "loss_streak=3 → pausing new entries for 30 min")
        else:
            state["reduce_size_factor"] = 0.5
            append_log("WARN", "RISK", "loss_streak=3 (GOD mode — halving size, no pause)")
    elif streak >= 2:
        if not god:
            state["reduce_size_factor"] = 0.5
            append_log("WARN", "RISK", "loss_streak=2 → reducing entry aggressiveness")
        else:
            state["reduce_size_factor"] = 0.75
            append_log("WARN", "RISK", "loss_streak=2 (GOD mode — reducing size to 75%)")
    else:
        # Gradual size recovery: each win adds 0.25 back toward 1.0.
        # Prevents a scratch trade from immediately restoring full size after 3 losses.
        prev_factor = float(state.get("reduce_size_factor") or 1.0)
        state["reduce_size_factor"] = min(1.0, prev_factor + 0.25)
        if prev_factor < 1.0 and state["reduce_size_factor"] >= 1.0:
            append_log("INFO", "RISK", "consecutive wins → full size restored")
        elif prev_factor < 1.0:
            append_log("INFO", "RISK", f"win → size partially restored to {state['reduce_size_factor']:.2f}")
        # A profitable win breaks the streak — clear halt/pause flags so the bot
        # can resume trading normally mid-day without waiting for next day rollover.
        # Scratch (result=0) does not clear halt — only a genuine profit does.
        # Note: daily_loss_cap halt is intentionally NOT cleared here — that
        # requires manual /resetday or next-day rollover.
        if result > 0 and int(state.get("loss_streak") or 0) == 0:
            if state.get("halt_for_day") and not state.get("day_guard_reason"):
                now = datetime.now(IST)
                too_late = now.hour > 14 or (now.hour == 14 and now.minute >= 30)
                if too_late:
                    append_log("INFO", "RISK", "loss_streak cleared by win but halt_for_day preserved — too late in session to resume")
                else:
                    state["halt_for_day"] = False
                    state["pause_entries_until"] = None
                    append_log("INFO", "RISK", "loss_streak cleared by win → halt_for_day lifted")


def check_day_drawdown_guard(state: dict, risk_profile: str = "STANDARD"):
    pnl = float(state.get("today_pnl") or 0.0)
    peak = float(state.get("day_peak_pnl") or 0.0)
    if pnl > peak:
        peak = pnl
        state["day_peak_pnl"] = peak
        append_log("INFO", "DAY", f"peak_pnl={peak:.2f}")
    realized = float(state.get("realized_today") or 0.0)
    unrealized = float(state.get("unrealized_now") or 0.0)
    giveback = max(0.0, peak - pnl)
    state["day_giveback_inr"] = giveback
    state["day_guard_reason"] = ""
    append_log("INFO", "DAY", f"day_guard_eval pnl={pnl:.2f} peak={peak:.2f} realized={realized:.2f} unrealized={unrealized:.2f} giveback={giveback:.2f}")

    god = str(risk_profile or "STANDARD").upper() == "GOD"

    # Daily loss cap — applies in all modes (hard broker/capital reality).
    loss_cap = abs(float(state.get("daily_loss_cap_inr") or getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0) or 200.0))
    if loss_cap > 0 and pnl <= -loss_cap:
        state["halt_for_day"] = True
        state["day_guard_reason"] = "daily_loss_guard"
        append_log("WARN", "DAY", f"daily_loss_guard triggered loss={pnl:.2f} cap={loss_cap:.2f} action=halt_for_day")
        return False

    if peak <= 0:
        return True

    # Don't fire giveback guard on trivially small peaks — they're just noise
    # relative to wallet size and would lock entries for the rest of the day.
    min_peak = float(getattr(CFG, "GOD_MIN_PEAK_FOR_GIVEBACK_INR" if god else "MIN_PEAK_FOR_GIVEBACK_INR", 200.0 if god else 150.0) or (200.0 if god else 150.0))
    if peak < min_peak:
        return True

    dd_pct = (giveback / peak) * 100.0 if peak > 0 else 0.0
    # GOD mode gets relaxed (but not removed) profit-giveback thresholds.
    if god:
        halt_pct  = float(getattr(CFG, "GOD_DAY_PROFIT_GIVEBACK_HALT_PCT",  75.0) or 75.0)
        pause_pct = float(getattr(CFG, "GOD_DAY_PROFIT_GIVEBACK_PAUSE_PCT", 55.0) or 55.0)
        reduce_pct = float(getattr(CFG, "GOD_DAY_PROFIT_GIVEBACK_REDUCE_PCT", 35.0) or 35.0)
    else:
        halt_pct  = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_HALT_PCT",  60.0) or 60.0)
        pause_pct = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_PAUSE_PCT", 40.0) or 40.0)
        reduce_pct = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_REDUCE_PCT", 25.0) or 25.0)

    # Log idempotency (audit fix 2026-05-04): the giveback guard runs every
    # tick (~20s). Without state-change detection it re-emits the WARN line
    # endlessly — 1,897 occurrences observed in a single session. We log
    # once per action transition (e.g. None→reduce_size, reduce_size→pause_entries).
    last_action = str(state.get("_giveback_log_last_action") or "")

    def _log_giveback_once(action: str):
        signature = f"{action}:{int(round(dd_pct))}"
        if last_action != signature:
            append_log("WARN", "DAY", f"profit_giveback_guard triggered dd_pct={dd_pct:.1f} action={action}")
            state["_giveback_log_last_action"] = signature

    if dd_pct >= halt_pct:
        state["halt_for_day"] = True
        state["day_guard_reason"] = "profit_giveback_guard"
        _log_giveback_once("halt_for_day")
        return False
    if dd_pct >= pause_pct:
        state["pause_entries_until"] = datetime.now(IST) + timedelta(minutes=30)
        state["reduce_size_factor"] = 0.5
        state["day_guard_reason"] = "profit_giveback_guard"
        _log_giveback_once("pause_entries")
        return False
    if dd_pct >= reduce_pct:
        state["reduce_size_factor"] = 0.5
        state["day_guard_reason"] = "profit_giveback_guard"
        _log_giveback_once("reduce_size")
    else:
        # Below all giveback thresholds → reset the log signature so the
        # next transition above threshold re-emits.
        if last_action:
            state["_giveback_log_last_action"] = ""
    return True
