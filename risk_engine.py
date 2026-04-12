import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as CFG
from broker_zerodha import get_kite
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")


def sync_wallet(state: dict):
    now = datetime.now(IST)
    last = state.get("last_wallet_sync_ts")
    interval = int(getattr(CFG, "WALLET_SYNC_INTERVAL_SEC", 120))
    night_interval = int(getattr(CFG, "WALLET_NIGHT_SYNC_INTERVAL_SEC", 900))
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    in_market = start <= now <= end
    min_interval = interval if in_market else night_interval
    if last and (now - last) < timedelta(seconds=min_interval):
        if not in_market:
            append_log("INFO", "WALLET", "Night skip → cached wallet used")
        return get_wallet_safe(state)

    retries = int(getattr(CFG, "WALLET_SYNC_RETRIES", 3))
    base = float(getattr(CFG, "WALLET_RETRY_BASE_SEC", 1.5))
    for attempt in range(retries):
        try:
            data = get_kite().margins() or {}
            eq = data.get("equity", {}) if isinstance(data, dict) else {}
            wallet = float(eq.get("net") or state.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
            avail = eq.get("available", {}) if isinstance(eq, dict) else {}
            if isinstance(avail, dict):
                w_av = float(avail.get("live_balance") or avail.get("cash") or avail.get("opening_balance") or wallet)
            else:
                w_av = wallet
            state["last_wallet"] = wallet
            state["wallet_net_inr"] = max(0.0, wallet)
            state["wallet_available_inr"] = max(0.0, w_av)
            state["last_wallet_sync_ts"] = now
            append_log("INFO", "WALLET", f"Synced wallet={wallet:.2f}")
            return wallet
        except Exception as e:
            append_log("WARNING", "WALLET", f"Attempt {attempt + 1} failed: {e}")
            if attempt + 1 < retries:
                append_log("WARNING", "WALLET", f"Retry {attempt + 2}/{retries}")
                time.sleep(base * (attempt + 1))

    wallet = get_wallet_safe(state)
    state["wallet_net_inr"] = wallet
    state["wallet_available_inr"] = wallet
    state["last_wallet_sync_ts"] = now
    append_log("WARNING", "WALLET", f"API failed → using cached wallet={wallet:.2f}")
    return wallet


def get_wallet_safe(state: dict):
    return float(state.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)


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


def can_enter_trade(symbol: str, price: float, positions: dict, wallet: float, qty: int, *, sector: str | None = None):
    required = float(price) * max(1, int(qty or 1))
    current = get_current_exposure(positions)
    max_exp = wallet * float(getattr(CFG, "MAX_EXPOSURE_PCT", 75.0)) / 100.0
    if current + required > max_exp:
        append_log("INFO", "SKIP", f"{symbol} reason=exposure next={current+required:.2f} max={max_exp:.2f}")
        return False
    return check_sector_exposure(symbol, positions, sector=sector)


def update_loss_streak(state: dict, result: float):
    streak = int(state.get("loss_streak") or 0)
    wins = int(state.get("consecutive_wins") or 0)
    if result < 0:
        streak += 1
        wins = 0
    else:
        streak = 0
        wins += 1
    state["loss_streak"] = streak
    state["consecutive_wins"] = wins

    if streak >= 4:
        state["halt_for_day"] = True
        append_log("WARN", "RISK", "loss_streak=4 → stopping new trades for the day")
    elif streak >= 3:
        state["pause_entries_until"] = datetime.now(IST) + timedelta(minutes=30)
        state["reduce_size_factor"] = 0.5
        append_log("WARN", "RISK", "loss_streak=3 → pausing new entries for 30 min")
    elif streak >= 2:
        state["reduce_size_factor"] = 0.5
        append_log("WARN", "RISK", "loss_streak=2 → reducing entry aggressiveness")
    else:
        state["reduce_size_factor"] = 1.0
        # A win breaks the streak — clear halt/pause flags so the bot can resume
        # trading normally mid-day without waiting for next day rollover.
        # Note: daily_loss_cap halt is intentionally NOT cleared here — that
        # requires manual /resetday or next-day rollover.
        if result >= 0 and int(state.get("loss_streak") or 0) == 0:
            if state.get("halt_for_day") and not state.get("day_guard_reason"):
                state["halt_for_day"] = False
                state["pause_entries_until"] = None
                append_log("INFO", "RISK", "loss_streak cleared by win → halt_for_day lifted")


def check_day_drawdown_guard(state: dict):
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

    loss_cap = abs(float(state.get("daily_loss_cap_inr") or getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0) or 200.0))
    if loss_cap > 0 and pnl <= -loss_cap:
        state["halt_for_day"] = True
        state["day_guard_reason"] = "daily_loss_guard"
        append_log("WARN", "DAY", f"daily_loss_guard triggered loss={pnl:.2f} cap={loss_cap:.2f} action=halt_for_day")
        return False

    if peak <= 0:
        return True

    dd_pct = (giveback / peak) * 100.0 if peak > 0 else 0.0
    halt_pct = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_HALT_PCT", 60.0) or 60.0)
    pause_pct = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_PAUSE_PCT", 40.0) or 40.0)
    reduce_pct = float(getattr(CFG, "DAY_PROFIT_GIVEBACK_REDUCE_PCT", 25.0) or 25.0)

    if dd_pct >= halt_pct:
        state["halt_for_day"] = True
        state["day_guard_reason"] = "profit_giveback_guard"
        append_log("WARN", "DAY", f"profit_giveback_guard triggered dd_pct={dd_pct:.1f} action=halt_for_day")
        return False
    if dd_pct >= pause_pct:
        state["pause_entries_until"] = datetime.now(IST) + timedelta(minutes=30)
        state["reduce_size_factor"] = 0.5
        state["day_guard_reason"] = "profit_giveback_guard"
        append_log("WARN", "DAY", f"profit_giveback_guard triggered dd_pct={dd_pct:.1f} action=pause_entries")
        return False
    if dd_pct >= reduce_pct:
        state["reduce_size_factor"] = 0.5
        state["day_guard_reason"] = "profit_giveback_guard"
        append_log("WARN", "DAY", f"profit_giveback_guard triggered dd_pct={dd_pct:.1f} action=reduce_size")
    return True
