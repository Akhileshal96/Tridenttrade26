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
            wallet = float(eq.get("net") or 0.0)
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
    if wallet < 5000:
        return 500.0
    if wallet <= 15000:
        return 5000.0
    if wallet <= 30000:
        return 7000.0
    if wallet <= 60000:
        return 10000.0
    if wallet <= 100000:
        return 15000.0
    return 20000.0


def get_position_size(price: float, wallet: float):
    bucket = get_bucket_from_slab(wallet)
    bucket_qty = int(bucket / price) if price > 0 else 0
    risk_amt = bucket * float(getattr(CFG, "RISK_PER_TRADE_PCT", 1.0)) / 100.0
    per_share_risk = price * float(getattr(CFG, "STOPLOSS_PCT", 2.0)) / 100.0
    risk_qty = int(risk_amt / per_share_risk) if per_share_risk > 0 else 0
    qty = min(bucket_qty, risk_qty)
    return max(0, qty), bucket, bucket_qty, risk_qty


def get_current_exposure(positions: dict):
    total = 0.0
    for t in (positions or {}).values():
        e = float(t.get("entry") or t.get("entry_price") or 0.0)
        q = int(t.get("qty") or t.get("quantity") or 0)
        total += e * q
    return total


def can_enter_trade(symbol: str, price: float, positions: dict, wallet: float, qty: int):
    required = float(price) * max(1, int(qty or 1))
    current = get_current_exposure(positions)
    max_exp = wallet * float(getattr(CFG, "MAX_EXPOSURE_PCT", 60.0)) / 100.0
    if current + required > max_exp:
        append_log("INFO", "SKIP", f"{symbol} reason=exposure next={current+required:.2f} max={max_exp:.2f}")
        return False
    return True
