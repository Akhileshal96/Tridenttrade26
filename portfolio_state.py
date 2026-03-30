from datetime import datetime
from zoneinfo import ZoneInfo
import config as CFG

IST = ZoneInfo("Asia/Kolkata")

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "positions": {},
    "open_trades": {},
    "today_pnl": 0.0,
    "day_key": datetime.now(IST).strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
    "wallet_net_inr": 0.0,
    "wallet_available_inr": 0.0,
    "last_wallet": float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0),
    "daily_loss_cap_inr": float(getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0)),
    "daily_profit_milestone_inr": float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 90.0)),
    "profit_milestone_hit": False,
    "last_wallet_sync_ts": None,
    "cooldown_until": None,
    "last_exit_ts": {},
    "skip_cooldown": {},
}


def _normalize_trade(symbol: str, trade: dict):
    symbol = (symbol or "").strip().upper()
    if not symbol or not isinstance(trade, dict):
        return None
    entry = float(trade.get("entry") or trade.get("entry_price") or 0.0)
    qty = int(trade.get("qty") or trade.get("quantity") or 1)
    peak = float(trade.get("peak") or trade.get("peak_pct") or 0.0)
    trail_active = bool(trade.get("trail_active", trade.get("trailing_active", False)))
    return {
        "entry": entry,
        "entry_price": entry,
        "qty": qty,
        "quantity": qty,
        "peak": peak,
        "peak_pct": peak,
        "trail_active": trail_active,
        "trailing_active": trail_active,
        "order_id": trade.get("order_id"),
    }


def _ensure_positions():
    positions = STATE.setdefault("positions", {})
    legacy_map = STATE.get("open_trades")
    if isinstance(legacy_map, dict) and legacy_map is not positions:
        for sym, tr in legacy_map.items():
            norm = _normalize_trade(sym, tr)
            if norm and sym not in positions:
                positions[sym] = norm

    legacy_single = STATE.get("open_trade")
    if isinstance(legacy_single, dict) and not positions:
        sym = str(legacy_single.get("symbol") or "").strip().upper()
        norm = _normalize_trade(sym, legacy_single)
        if norm:
            positions[sym] = norm

    STATE["open_trades"] = positions
    return positions


def add_position(symbol: str, position_data: dict):
    positions = _ensure_positions()
    norm = _normalize_trade(symbol, position_data)
    if norm:
        positions[symbol.strip().upper()] = norm


def remove_position(symbol: str):
    positions = _ensure_positions()
    positions.pop((symbol or "").strip().upper(), None)


def get_positions():
    return _ensure_positions()


def get_position(symbol: str):
    return _ensure_positions().get((symbol or "").strip().upper())


def positions_count():
    return len(_ensure_positions())
