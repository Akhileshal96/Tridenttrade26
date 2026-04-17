import math
import os
import time
import threading
import inspect
from collections import deque
from datetime import datetime, timedelta, time as dt_time
from urllib.parse import urljoin
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log
import strategy_engine as SE
from strategy_engine import generate_signal, generate_mean_reversion_signal, generate_vwap_ema_signal, generate_pullback_signal
from excluded_store import load_excluded, add_symbol, remove_symbol
from execution_engine import (
    monitor_positions as ee_monitor_positions,
    process_entries as ee_process_entries,
    force_exit_all as ee_force_exit_all,
    _calc_trail_activate_inr as ee_calc_trail_activate_inr,
    _dynamic_trail_levels as ee_dynamic_trail_levels,
)
import risk_engine as RISK
import research_engine as RE
from universe_builder import SECTOR_MAP
from market_regime import get_market_regime_snapshot, get_regime_entry_mode
import strategy_analytics as SA
from state_lock import STATE_LOCK, safe_update, safe_set, PositionManager

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "positions": {},  # SYMBOL -> trade dict
    "today_pnl": 0.0,
    "day_key": datetime.now(IST).strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
    "wallet_net_inr": 0.0,
    "wallet_available_inr": 0.0,
    "last_wallet": float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0),
    "daily_loss_cap_inr": float(getattr(CFG, "DAILY_LOSS_CAP_INR", 300.0)),
    "daily_profit_milestone_inr": float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 200.0)),
    "profit_milestone_hit": False,
    "last_wallet_sync_ts": None,
    "wallet_cached_mode_until": None,
    "wallet_cached_mode_reason": "",
    "cooldown_until": None,
    "last_exit_ts": {},
    "skip_cooldown": {},
    "loss_streak": 0,
    "consecutive_wins": 0,
    "reduce_size_factor": 1.0,
    "pause_entries_until": None,
    "halt_for_day": False,
    "day_peak_pnl": 0.0,
    "sector_map_cache": None,
    "research_universe": [],
    "fallback_universe": [],
    "active_universe": [],
    "active_universe_last_refresh": None,
    "active_no_setup_cycles": 0,
    "opening_mode": "OPEN_CLEAN",
    "opening_metrics": {},
    "open_feed_retry_count": 0,
    "mean_reversion_dry_cycles": 0,
    "entry_tier_for_cooldown": None,
    "no_entry_cycles": 0,
    "top3_dry_cycles": 0,
    "fallback_mode_active": False,
    "active_strategy_families": [],
    "active_strategy_last_refresh": None,
    "active_strategy_last_reason": "",
    "strategy_scores_last": {},
    "strategy_selection_history": [],
    "last_route_universe_source": "n/a",
    "last_trend_direction": "UNKNOWN",
    "eod_report_sent_date": "",
    "confirm_strictness": "STRICT",
    "signals_seen_window": 0,
    "entries_executed_window": 0,
    "signal_event_ts": [],
    "entry_event_ts": [],
    "micro_mode_active": False,
    "micro_mode_trade_count": 0,
    "micro_mode_regime": "",
    "realized_today": 0.0,
    "unrealized_now": 0.0,
    "pnl_so_far": 0.0,
    "trade_events": [],
    "research_events": [],
    "universe_changes_today": [],
    "route_changes_today": [],
    "recent_entries": [],
    "recent_exits": [],
    "last_short_reject_reasons": {},
    "force_exit_done": False,
    "ip_current": "",
    "ip_expected": str(getattr(CFG, "KITE_STATIC_IP", "") or "").strip(),
    "ip_compliant": False,
    "ip_last_error": "",
    "ip_last_check_ts": 0.0,
    "ip_manual_rearm_required": False,
    "live_order_allowed": False,
    # Trading mode: INTRADAY (MIS, force exit 15:10) | SWING (CNC longs only) | HYBRID (per-trade)
    "trading_mode": str(os.getenv("TRADING_MODE", "INTRADAY")).strip().upper(),
    # Risk profile: STANDARD (current safe behavior) | GOD (neutralizes bot-imposed soft caps)
    "risk_profile": str(os.getenv("RISK_PROFILE", "STANDARD")).strip().upper(),
    # Pending two-step confirmation state for GOD activation (cleared on confirm/cancel/standard switch)
    "pending_risk_profile_confirmation": None,
}

# backwards compatibility for any caller that still checks open_trades key
STATE["open_trades"] = STATE["positions"]
PM = PositionManager(STATE)

RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(getattr(CFG, "MAX_ENTRY_SLIPPAGE_PCT", 0.30)),
    "BUCKET_MODE": str(getattr(CFG, "BUCKET_MODE", "PCT")).upper(),
    "BUCKET_PCT": float(getattr(CFG, "BUCKET_PCT", 10.0)),
    "BUCKET_INR": float(getattr(CFG, "BUCKET_INR", 1000.0)),
    "BUCKET_MIN_INR": float(getattr(CFG, "BUCKET_MIN_INR", 1000.0)),
    "BUCKET_MAX_INR": float(getattr(CFG, "BUCKET_MAX_INR", 5000.0)),
    "MAX_EXPOSURE_PCT": float(getattr(CFG, "MAX_EXPOSURE_PCT", 75.0)),
    "USE_BUCKET_SLABS": bool(getattr(CFG, "USE_BUCKET_SLABS", True)),
    "SOFT_PROFIT_TARGET": str(os.getenv("SOFT_PROFIT_TARGET", "true")).strip().lower() == "true",
}

_ORDER_RATE_LOCK = threading.Lock()
_ORDER_REQ_TS = deque(maxlen=256)
_KITE_MP_SUPPORT_CACHE = {}

# ---------------------------------------------------------------------------
# STATE persistence helpers
# ---------------------------------------------------------------------------

_STATE_SNAPSHOT_PATH = os.path.join(DATA_DIR, "state_snapshot.json")

# Keys that survive a restart (positions, daily risk counters, halt/pause flags).
# Transient UI state and in-tick caches are intentionally excluded.
_STATE_PERSIST_KEYS = [
    "day_key",
    "today_pnl",
    "halt_for_day",
    "loss_streak",
    "consecutive_wins",
    "reduce_size_factor",
    "day_peak_pnl",
    "profit_milestone_hit",
    "cooldown_until",
    "pause_entries_until",
    "positions",
    "trading_mode",
    "risk_profile",
]


def _save_state_snapshot() -> None:
    """Atomically write critical STATE fields to disk."""
    import json
    try:
        snap: dict = {}
        with STATE_LOCK:
            for k in _STATE_PERSIST_KEYS:
                v = STATE.get(k)
                if isinstance(v, datetime):
                    snap[k] = v.isoformat()
                elif isinstance(v, dict):
                    snap[k] = dict(v)
                else:
                    snap[k] = v
        tmp = _STATE_SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snap, fh, default=str)
        os.replace(tmp, _STATE_SNAPSHOT_PATH)
    except Exception as exc:
        # Persistence failure is non-fatal; log and continue.
        try:
            from log_store import append_log as _al
            _al("WARN", "STATE", f"state_snapshot_save_failed: {exc}")
        except Exception:
            pass


def _load_state_snapshot() -> None:
    """Restore persisted STATE fields on startup.

    Only restores if the snapshot was saved for today's trading date.
    """
    import json
    try:
        if not os.path.exists(_STATE_SNAPSHOT_PATH):
            return
        with open(_STATE_SNAPSHOT_PATH, "r") as fh:
            snap: dict = json.load(fh)
        saved_day = str(snap.get("day_key") or "")
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if saved_day != today:
            append_log("INFO", "STATE", f"state_snapshot stale (saved={saved_day} today={today}) — skipping restore")
            return
        with STATE_LOCK:
            for k in _STATE_PERSIST_KEYS:
                if k not in snap:
                    continue
                v = snap[k]
                if k in ("cooldown_until", "pause_entries_until") and v:
                    try:
                        parsed = datetime.fromisoformat(str(v))
                        # Ensure timezone-aware in IST
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=IST)
                        else:
                            parsed = parsed.astimezone(IST)
                        STATE[k] = parsed
                    except Exception:
                        pass
                elif k == "positions" and isinstance(v, dict):
                    STATE["positions"].update(v)
                    STATE["open_trades"] = STATE["positions"]
                else:
                    STATE[k] = v
        append_log("INFO", "STATE", f"state_snapshot restored day={saved_day} pnl={snap.get('today_pnl')} positions={len(snap.get('positions') or {})}")
    except Exception as exc:
        append_log("WARN", "STATE", f"state_snapshot_load_failed: {exc}")


def _cfg_obj():
    """Runtime-safe config resolver for loop/thread contexts."""
    try:
        return CFG
    except NameError:
        import config as cfg_mod
        globals()["CFG"] = cfg_mod
        append_log("INFO", "CONFIRM", "using config default for CFG_module_binding=runtime_reload")
        return cfg_mod


def _cfg_get(name: str, default):
    cfg = _cfg_obj()
    # GOD profile overrides: check GOD_<NAME> first for specific keys when active.
    try:
        profile = str(STATE.get("risk_profile") or "STANDARD").upper()
    except Exception:
        profile = "STANDARD"
    if profile == "GOD":
        god_name = f"GOD_{name}"
        god_val = getattr(cfg, god_name, None)
        if god_val is not None:
            return god_val
    val = getattr(cfg, name, None)
    if val is None:
        append_log("INFO", "CONFIRM", f"using config default for {name}={default}")
        return default
    return val

STRATEGY_REGISTRY = [
    {
        "family": "trend_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "TRENDING"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_signal",
        "class": "primary",
    },
    {
        "family": "pullback_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "TRENDING"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_pullback_signal",
        "class": "primary",
    },
    {
        "family": "short_breakdown",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE", "OPEN_UNSAFE"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "primary",
    },
    {
        "family": "mean_reversion",
        "direction": "long",
        "preferred_regimes": ["SIDEWAYS", "UNKNOWN"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_mean_reversion_signal",
        "class": "primary",
    },
    {
        "family": "fallback_long",
        "direction": "long",
        "preferred_regimes": ["UNKNOWN", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_mean_reversion_signal",
        "class": "fallback",
    },
    {
        "family": "fallback_short",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN", "VOLATILE", "UNKNOWN"],
        "preferred_market_structure": ["OPEN_MODERATE", "OPEN_UNSAFE", "OPEN_CLEAN"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "fallback",
    },
    {
        "family": "outlier_long",
        "direction": "long",
        "preferred_regimes": ["TRENDING_UP", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE"],
        "scan_function_name": "_scan_long_entries",
        "signal_function_name": "generate_vwap_ema_signal",
        "class": "outlier",
    },
    {
        "family": "outlier_short",
        "direction": "short",
        "preferred_regimes": ["WEAK", "TRENDING_DOWN", "SIDEWAYS", "VOLATILE"],
        "preferred_market_structure": ["OPEN_CLEAN", "OPEN_MODERATE", "OPEN_UNSAFE"],
        "scan_function_name": "_scan_short_entries",
        "signal_function_name": "generate_short_signal",
        "class": "outlier",
    },
]

_NOTIFIER = None


def set_notifier(fn):
    global _NOTIFIER
    _NOTIFIER = fn


def _notify(msg: str):
    if not _NOTIFIER:
        return
    try:
        _NOTIFIER(msg)
    except Exception as e:
        append_log("WARN", "NOTIFY", f"Notifier error: {e}")


def _append_runtime_event(bucket: str, payload: dict, limit: int = 300):
    if not isinstance(payload, dict):
        return
    ts = datetime.now(IST).isoformat(timespec="seconds")
    rec = {"ts": ts}
    rec.update(payload)
    events = STATE.setdefault(bucket, [])
    if not isinstance(events, list):
        events = []
        STATE[bucket] = events
    events.append(rec)
    if len(events) > limit:
        del events[:-limit]


def _log_trade_event(event_tag: str, trade: dict):
    tr = dict(trade or {})
    sym = str(tr.get("symbol") or "").strip().upper()
    side = str(tr.get("side") or "BUY").upper()
    fam = str(tr.get("strategy_family") or "unknown")
    tier = str(tr.get("confidence_tier") or "n/a")
    qty = int(tr.get("qty") or tr.get("quantity") or 0)
    entry = float(tr.get("entry") or tr.get("entry_price") or 0.0)
    exit_px = float(tr.get("exit_price") or tr.get("exit") or 0.0) if tr.get("exit_price") is not None or tr.get("exit") is not None else None
    oid = tr.get("order_id")
    entry_ts = str(tr.get("entry_time") or "-")
    exit_ts = str(tr.get("exit_time") or "-")
    exit_reason = str(tr.get("exit_reason") or tr.get("reason") or "-")
    parts = [
        f"symbol={sym}",
        f"side={side}",
        f"family={fam}",
        f"tier={tier}",
        f"qty={qty}",
        f"entry={entry:.2f}",
        f"order_id={oid or '-'}",
        f"entry_ts={entry_ts}",
    ]
    if exit_px is not None:
        parts.append(f"exit={float(exit_px):.2f}")
    parts.append(f"exit_ts={exit_ts}")
    parts.append(f"exit_reason={exit_reason}")
    append_log("INFO", event_tag, " ".join(parts))
    _append_runtime_event("trade_events", {"event": event_tag, **tr})


def _migrate_legacy_positions():
    """One-time migration of legacy STATE layouts to the current positions dict.

    Called once at startup (run_loop_forever) so _positions() stays a pure read.
    Safe to call multiple times — migrated entries are skipped on repeat calls.
    """
    pos = STATE.setdefault("positions", {})

    def _normalize(sym: str, tr: dict):
        if not sym or not isinstance(tr, dict):
            return None
        entry = float(tr.get("entry") or tr.get("entry_price") or 0.0)
        qty = int(tr.get("qty") or tr.get("quantity") or 1)
        peak = float(tr.get("peak") or tr.get("peak_pct") or 0.0)
        trail_active = bool(tr.get("trail_active", tr.get("trailing_active", False)))
        peak_pnl_inr = float(tr.get("peak_pnl_inr") or 0.0)
        return {
            "entry": entry,
            "entry_price": entry,
            "qty": qty,
            "quantity": qty,
            "peak": peak,
            "peak_pct": peak,
            "peak_pnl_inr": peak_pnl_inr,
            "trail_active": trail_active,
            "trailing_active": trail_active,
            "order_id": tr.get("order_id"),
        }

    # merge legacy multi-trade map
    legacy_map = STATE.get("open_trades")
    if isinstance(legacy_map, dict) and legacy_map is not pos:
        migrated = 0
        for raw_sym, tr in legacy_map.items():
            sym = str(raw_sym or "").strip().upper()
            if not sym or sym in pos:
                continue
            norm = _normalize(sym, tr)
            if norm:
                pos[sym] = norm
                migrated += 1
        if migrated:
            append_log("INFO", "STATE", f"Merged {migrated} legacy open_trades into positions")

    # migrate legacy single trade slot
    legacy = STATE.get("open_trade")
    if legacy and isinstance(legacy, dict):
        sym = str(legacy.get("symbol") or "").strip().upper()
        if sym and sym not in pos:
            norm = _normalize(sym, legacy)
            if norm:
                pos[sym] = norm
                append_log("INFO", "STATE", f"Migrated legacy open_trade -> positions for {sym}")

    # ensure trailing keys exist
    for tr in pos.values():
        if not isinstance(tr, dict):
            continue
        tr.setdefault("peak_pnl_inr", 0.0)
        tr.setdefault("trail_active", bool(tr.get("trailing_active", False)))
        tr.setdefault("trailing_active", bool(tr.get("trail_active", False)))

    STATE["open_trades"] = pos


def _positions():
    """Return the current open positions dict. Pure read — no side effects."""
    return STATE.setdefault("positions", {})


def _trade_entry_qty(trade: dict) -> tuple[float, int]:
    entry = float((trade or {}).get("entry_price") or (trade or {}).get("entry") or 0.0)
    qty = int((trade or {}).get("quantity") or (trade or {}).get("qty") or 0)
    return entry, (qty if qty > 0 else 1)


def _parse_hhmm(s):
    try:
        hh, mm = str(s).strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0




def _load_sector_map():
    mp = {}
    path = os.getenv("SECTOR_MAP_PATH", os.path.join(DATA_DIR, "sector_map.csv"))
    if not os.path.exists(path):
        return mp
    try:
        with open(path, "r") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or "," not in ln:
                    continue
                sym, sec = ln.split(",", 1)
                sym = sym.strip().upper()
                sec = sec.strip().upper()
                if sym and sec:
                    mp[sym] = sec
    except Exception:
        return {}
    return mp


def _sector_for_symbol(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return "UNKNOWN"
    mp = STATE.setdefault("sector_map_cache", None)
    if mp is None:
        mp = _load_sector_map()
        STATE["sector_map_cache"] = mp
    return mp.get(sym, "UNKNOWN")
def _past_force_exit_time():
    now = datetime.now(IST)
    fh, fm = _parse_hhmm(getattr(CFG, "FORCE_EXIT", "15:10"))
    cutoff = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return now >= cutoff


def _ensure_day_key():
    with STATE_LOCK:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if STATE.get("day_key") != today:
            STATE["day_key"] = today
            STATE["today_pnl"] = 0.0
            # Preserve CNC (swing) positions across day rollover — they hold overnight.
            # Only clear MIS (intraday) positions which should have been force-exited.
            cnc_kept = {}
            for sym, trade in _positions().items():
                if str((trade or {}).get("product") or "MIS").upper() == "CNC":
                    cnc_kept[sym] = trade
            _positions().clear()
            if cnc_kept:
                _positions().update(cnc_kept)
                append_log("INFO", "DAY", f"Preserved {len(cnc_kept)} CNC positions across day rollover: {','.join(cnc_kept.keys())}")
            STATE["profit_milestone_hit"] = False
            STATE["cooldown_until"] = None
            STATE["loss_streak"] = 0
            STATE["consecutive_wins"] = 0
            STATE["reduce_size_factor"] = 1.0
            STATE["pause_entries_until"] = None
            STATE["halt_for_day"] = False
            STATE["day_peak_pnl"] = 0.0
            STATE["sector_map_cache"] = None
            STATE["open_feed_retry_count"] = 0
            STATE["top3_dry_cycles"] = 0
            STATE["active_strategy_families"] = []
            STATE["active_strategy_last_refresh"] = None
            STATE["active_strategy_last_reason"] = ""
            STATE["strategy_scores_last"] = {}
            STATE["strategy_selection_history"] = []
            STATE["last_route_universe_source"] = "n/a"
            STATE["last_trend_direction"] = "UNKNOWN"
            STATE["force_exit_done"] = False
            append_log("INFO", "DAY", f"Auto rollover reset for {today}")


def set_runtime_param(key, value):
    RUNTIME[key] = value


def manual_reset_day():
    with STATE_LOCK:
        STATE["today_pnl"] = 0.0
        _positions().clear()
        STATE["day_key"] = datetime.now(IST).strftime("%Y-%m-%d")
        STATE["profit_milestone_hit"] = False
        STATE["cooldown_until"] = None
        STATE["loss_streak"] = 0
        STATE["consecutive_wins"] = 0
        STATE["reduce_size_factor"] = 1.0
        STATE["pause_entries_until"] = None
        STATE["halt_for_day"] = False
        STATE["day_peak_pnl"] = 0.0
        STATE["sector_map_cache"] = None
        STATE["top3_dry_cycles"] = 0
        STATE["active_strategy_families"] = []
        STATE["active_strategy_last_refresh"] = None
        STATE["active_strategy_last_reason"] = ""
        STATE["strategy_scores_last"] = {}
        STATE["strategy_selection_history"] = []
        STATE["last_route_universe_source"] = "n/a"
        STATE["last_trend_direction"] = "UNKNOWN"
        STATE["force_exit_done"] = False
    append_log("INFO", "DAY", "Manual day reset executed")
    return True


def is_live_enabled():
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))


def _fetch_public_ipv4(timeout_sec: float = 3.0) -> str:
    try:
        with urlopen("https://api.ipify.org", timeout=timeout_sec) as resp:
            ip = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
            return ip
    except Exception:
        return ""


def _ip_compliance_recheck_interval_sec() -> int:
    return max(30, int(_cfg_get("KITE_IP_RECHECK_SEC", 180) or 180))


def evaluate_ip_compliance(force: bool = False) -> bool:
    now_ts = time.time()
    last_ts = float(STATE.get("ip_last_check_ts") or 0.0)
    if (not force) and (now_ts - last_ts) < _ip_compliance_recheck_interval_sec():
        return bool(STATE.get("ip_compliant"))

    expected = str(_cfg_get("KITE_STATIC_IP", "") or "").strip()
    current = _fetch_public_ipv4()
    STATE["ip_last_check_ts"] = now_ts
    STATE["ip_expected"] = expected
    STATE["ip_current"] = current

    if not expected:
        STATE["ip_compliant"] = True
        STATE["ip_last_error"] = ""
        if not bool(STATE.get("live_order_allowed")):
            STATE["live_order_allowed"] = True
        return True

    if current and (current == expected):
        STATE["ip_compliant"] = True
        STATE["ip_last_error"] = ""
        if bool(STATE.get("ip_manual_rearm_required")):
            STATE["live_order_allowed"] = False
        return True

    STATE["ip_compliant"] = False
    STATE["ip_last_error"] = "public_ip_mismatch_or_unavailable"
    STATE["live_order_allowed"] = False
    STATE["ip_manual_rearm_required"] = True
    STATE["paused"] = True
    STATE["live_override"] = False
    append_log(
        "ERROR",
        "IP",
        f"IP compliance mismatch expected={expected or 'unset'} current={current or 'unknown'} -> live orders blocked and loop paused",
    )
    return False


def request_live_rearm() -> tuple[bool, str]:
    evaluate_ip_compliance(force=True)
    if not bool(STATE.get("ip_compliant")):
        return False, "IP mismatch: live order placement remains blocked."
    STATE["live_order_allowed"] = True
    STATE["ip_manual_rearm_required"] = False
    return True, "IP compliant. Live order placement re-armed."


def get_ip_status_text() -> str:
    evaluate_ip_compliance(force=True)
    return (
        "🌐 IP Status\n\n"
        f"- Current Public IP: {STATE.get('ip_current') or 'unknown'}\n"
        f"- Approved Static IP: {STATE.get('ip_expected') or 'not_set'}\n"
        f"- Compliance: {'PASS' if STATE.get('ip_compliant') else 'FAIL'}\n"
        f"- Live Order Placement Allowed: {bool(STATE.get('live_order_allowed'))}\n"
        f"- Manual Rearm Required: {bool(STATE.get('ip_manual_rearm_required'))}"
    )


def _order_rate_limit_wait():
    limit = max(1, int(_cfg_get("ORDER_RATE_LIMIT_PER_SEC", 10) or 10))
    while True:
        now = time.time()
        with _ORDER_RATE_LOCK:
            while _ORDER_REQ_TS and (now - _ORDER_REQ_TS[0]) >= 1.0:
                _ORDER_REQ_TS.popleft()
            if len(_ORDER_REQ_TS) < limit:
                _ORDER_REQ_TS.append(now)
                return
            wait_s = max(0.01, 1.0 - (now - _ORDER_REQ_TS[0]))
        time.sleep(wait_s)



def list_exclusions():
    s = load_excluded()
    if not s:
        return "✅ Excluded symbols: (none)"
    return "⛔ Excluded symbols:\n" + "\n".join(sorted(s))


def exclude_symbol(sym):
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /exclude SYMBOL"
    changed = add_symbol(sym)
    if changed:
        append_log("WARN", "EXCL", f"Excluded {sym}")
    return f"⛔ {sym} excluded permanently. (/include {sym} to release)"


def include_symbol(sym):
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /include SYMBOL"
    changed = remove_symbol(sym)
    if changed:
        append_log("INFO", "EXCL", f"Included back {sym}")
        return f"✅ {sym} released from exclusions."
    return f"ℹ️ {sym} was not in exclusions."


def _atomic_copy(src, dst):
    if not os.path.exists(src):
        return False
    ddir = os.path.dirname(dst)
    if ddir:
        os.makedirs(ddir, exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "r") as fsrc, open(tmp, "w") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp, dst)
    return True


def _load_universe_from(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


def load_universe_trading():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))

    if not os.path.exists(trade_path) and os.path.exists(live_path):
        _atomic_copy(live_path, trade_path)
        append_log("INFO", "PROMOTE", "Bootstrapped trading universe from live universe")

    syms = _load_universe_from(trade_path)
    excl = load_excluded()
    syms = [s for s in syms if s not in excl]

    try:
        syms = syms[: int(getattr(CFG, "UNIVERSE_SIZE", 30))]
    except Exception:
        pass
    return syms


def load_universe_live():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = load_excluded()
    return [s for s in syms if s not in excl]


def _parse_windows(win_str):
    windows = []
    if not win_str:
        return windows
    for p in [x.strip() for x in str(win_str).split(",") if x.strip()]:
        if "-" not in p:
            continue
        a, b = p.split("-", 1)
        windows.append((_parse_hhmm(a), _parse_hhmm(b)))
    return windows


def _in_any_promote_window():
    now = datetime.now(IST)
    for (ah, am), (bh, bm) in _parse_windows(getattr(CFG, "PROMOTE_WINDOWS", "")):
        start = now.replace(hour=ah, minute=am, second=0, microsecond=0)
        end = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        if start <= now <= end:
            return True
    return False


def _cooldown_ok():
    cd_min = float(getattr(CFG, "PROMOTE_COOLDOWN_MIN", 60))
    last = STATE.get("last_promote_ts")
    if not last:
        return True
    return (datetime.now(IST) - last) >= timedelta(minutes=cd_min)


def _top10_overlap_ratio(a, b):
    a10, b10 = a[:10], b[:10]
    if not a10 or not b10:
        return 0.0
    inter = len(set(a10).intersection(set(b10)))
    return float(inter) / float(min(len(a10), len(b10)))


def _market_stable():
    try:
        sym = getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES").strip().upper()
        token = token_for_symbol(sym)
        kite = get_kite()
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=5)
        data = kite.historical_data(token, from_dt, to_dt, "15minute")
        time.sleep(0.3)

        df = pd.DataFrame(data)
        if df.empty or not all(c in df.columns for c in ["high", "low", "close"]):
            return False
        tail = df.tail(10)
        if len(tail) < 8:
            return False
        rng_pct = ((tail["high"] - tail["low"]).astype(float) / tail["close"].astype(float)) * 100.0
        return float(rng_pct.mean()) <= float(getattr(CFG, "STABILITY_ATR_PCT_MAX", 0.35))
    except Exception as e:
        append_log("WARN", "STABLE", f"Stability check failed: {e}")
        return False


def promote_universe(reason="AUTO"):
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))
    live = _load_universe_from(live_path)
    trade = _load_universe_from(trade_path)
    if not live:
        STATE["last_promote_msg"] = "No live universe available"
        return False

    min_overlap = float(getattr(CFG, "PROMOTE_TOP10_OVERLAP_MIN", 0.60))
    overlap = _top10_overlap_ratio(live, trade) if trade else 1.0
    if trade and overlap < min_overlap:
        STATE["last_promote_msg"] = f"Blocked (overlap {overlap:.2f} < {min_overlap:.2f})"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
        return False

    ok = _atomic_copy(live_path, trade_path)
    if ok:
        STATE["last_promote_ts"] = datetime.now(IST)
        STATE["last_promote_msg"] = f"Promoted ({reason}) overlap={overlap:.2f}"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
    return ok


def _is_market_hours(now: datetime) -> bool:
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= now <= end


def _cached_wallet_value() -> float:
    cached = float(STATE.get("last_wallet") or 0.0)
    if cached > 0:
        return cached
    return float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)


def _sync_wallet_and_caps(force=False):
    now = datetime.now(IST)
    last = STATE.get("last_wallet_sync_ts")

    day_interval = int(getattr(CFG, "WALLET_SYNC_INTERVAL_SEC", 120))
    night_interval = int(getattr(CFG, "WALLET_NIGHT_SYNC_INTERVAL_SEC", 900))
    in_market = _is_market_hours(now)
    min_interval = day_interval if in_market else night_interval

    if not force and last and (now - last) < timedelta(seconds=min_interval):
        if not in_market:
            cached = _cached_wallet_value()
            STATE["wallet_net_inr"] = max(0.0, cached)
            STATE["wallet_available_inr"] = max(0.0, cached)
            append_log("INFO", "WALLET", "Night skip → cached wallet used")
        return

    retries = max(1, int(getattr(CFG, "WALLET_SYNC_RETRIES", 3)))
    backoff = float(getattr(CFG, "WALLET_RETRY_BASE_SEC", 1.5))
    auth_cooldown_sec = int(getattr(CFG, "WALLET_AUTH_COOLDOWN_SEC", 3600) or 3600)
    cached_until = STATE.get("wallet_cached_mode_until")
    if (not in_market) and (not force) and isinstance(cached_until, datetime) and now < cached_until:
        wallet_net = _cached_wallet_value()
        wallet_avail = wallet_net
        STATE["wallet_net_inr"] = max(0.0, wallet_net)
        STATE["wallet_available_inr"] = max(0.0, wallet_avail)
        return

    wallet_net = _cached_wallet_value()
    wallet_avail = wallet_net
    synced = False

    for attempt in range(retries):
        try:
            m = get_kite().margins() or {}
            eq = m.get("equity", {}) if isinstance(m, dict) else {}
            wallet_net = float(eq.get("net") or wallet_net or 0.0)
            avail = eq.get("available", {}) if isinstance(eq, dict) else {}
            if isinstance(avail, dict):
                wallet_avail = float(
                    avail.get("live_balance") or avail.get("cash") or avail.get("opening_balance") or avail.get("adhoc_margin") or wallet_net
                )
            else:
                wallet_avail = wallet_net
            STATE["last_wallet"] = max(0.0, wallet_net)
            append_log("INFO", "WALLET", f"Synced wallet={wallet_net:.2f}")
            if STATE.get("wallet_cached_mode_until"):
                append_log("INFO", "WALLET", "Recovered from cached-wallet mode")
            STATE["wallet_cached_mode_until"] = None
            STATE["wallet_cached_mode_reason"] = ""
            synced = True
            break
        except Exception as e:
            emsg = str(e)
            is_auth_error = any(x in emsg.lower() for x in ("token", "author", "login", "permission", "access"))
            if (not in_market) and is_auth_error:
                until = now + timedelta(seconds=max(300, auth_cooldown_sec))
                if not STATE.get("wallet_cached_mode_until"):
                    append_log("WARNING", "WALLET", f"Auth error off-market -> cached-wallet mode enabled until {until.isoformat(timespec='seconds')}")
                STATE["wallet_cached_mode_until"] = until
                STATE["wallet_cached_mode_reason"] = emsg[:200]
                break
            append_log("WARNING", "WALLET", f"Attempt {attempt + 1} failed: {e}")
            if attempt + 1 < retries:
                append_log("WARNING", "WALLET", f"Retry {attempt + 2}/{retries}")
                time.sleep(backoff * (attempt + 1))

    if not synced:
        wallet_net = _cached_wallet_value()
        wallet_avail = wallet_net
        append_log("WARNING", "WALLET", f"API failure → using cached wallet={wallet_net:.2f}")
        # Alert trader via Telegram if wallet sync fails during market hours
        # so they know position sizing may be based on stale data.
        if _is_market_hours(datetime.now(IST)):
            _notify(f"⚠️ Wallet sync failed — using cached ₹{wallet_net:.2f}\nPosition sizing may be inaccurate. Check Zerodha API.")

    if wallet_net <= 0:
        wallet_net = float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        wallet_avail = max(wallet_avail, wallet_net)
        append_log("WARNING", "WALLET", f"API failed → using cached wallet={wallet_net:.2f}")

    STATE["wallet_net_inr"] = max(0.0, wallet_net)
    STATE["wallet_available_inr"] = max(0.0, wallet_avail if wallet_avail > 0 else wallet_net)

    # Dynamic daily guards: percentage of wallet auto-fetched each sync.
    # Raised defaults: 3% loss cap (was 2%), 2% profit milestone (was 1%)
    # so a 10k wallet gets ₹300 loss cap / ₹200 profit milestone instead of
    # the old ₹200/₹90 which choked profitable days too early.
    loss_pct = float(os.getenv("DAILY_LOSS_CAP_PCT", "3.0"))
    prof_pct = float(os.getenv("DAILY_PROFIT_MILESTONE_PCT", os.getenv("DAILY_PROFIT_TARGET_PCT", "2.0")))
    if STATE["wallet_net_inr"] > 0:
        STATE["daily_loss_cap_inr"] = STATE["wallet_net_inr"] * loss_pct / 100.0
        STATE["daily_profit_milestone_inr"] = STATE["wallet_net_inr"] * prof_pct / 100.0
    else:
        STATE["daily_loss_cap_inr"] = float(getattr(CFG, "DAILY_LOSS_CAP_INR", 200.0))
        STATE["daily_profit_milestone_inr"] = float(getattr(CFG, "DAILY_PROFIT_TARGET_INR", 90.0))
    STATE["last_wallet_sync_ts"] = now


def _open_positions_count():
    return len(_positions())


def _current_exposure_inr():
    total = 0.0
    for t in _positions().values():
        e, q = _trade_entry_qty(t)
        total += e * q
    return total


def _effective_max_exposure_pct() -> float:
    """Effective exposure cap: GOD uses GOD_MAX_EXPOSURE_PCT when active; else
    STANDARD uses RUNTIME (which mirrors CFG) exactly as before."""
    try:
        profile = str(STATE.get("risk_profile") or "STANDARD").upper()
    except Exception:
        profile = "STANDARD"
    if profile == "GOD":
        god = getattr(CFG, "GOD_MAX_EXPOSURE_PCT", None)
        if god is not None:
            return float(god)
    return float(RUNTIME.get("MAX_EXPOSURE_PCT", 60.0))


def _max_exposure_inr():
    base = float(STATE.get("wallet_net_inr") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    return base * _effective_max_exposure_pct() / 100.0


def _bucket_inr(wallet_net: float) -> float:
    # Aggressive percentage-based bucket for concentrated quality trades.
    # Default 35% of wallet per trade — with max 3 concurrent on a 10k
    # wallet, this means ~₹3,500 per position, filling 75% exposure fast
    # with fewer, bigger bets on high-conviction setups.
    bucket_pct = max(1.0, float(_cfg_get("BUCKET_ALLOC_PCT", 35.0) or 35.0))
    bucket = wallet_net * bucket_pct / 100.0
    # Floor: at least 15% of wallet (no dust positions)
    # Ceiling: at most 50% of wallet (single trade can be half the wallet
    # on a FULL-confidence trending setup)
    floor_pct = max(1.0, float(_cfg_get("BUCKET_FLOOR_PCT", 15.0) or 15.0))
    ceil_pct = max(floor_pct + 1, float(_cfg_get("BUCKET_CEIL_PCT", 50.0) or 50.0))
    bmin = wallet_net * floor_pct / 100.0
    bmax = wallet_net * ceil_pct / 100.0
    return max(bmin, min(bucket, bmax))


def _dynamic_max_concurrent() -> int:
    """Scale max concurrent trades with wallet size.

    Concentrated approach: fewer, bigger trades for max profit.
    Small accounts get 2-3 positions, larger accounts top out at 5.
    """
    wallet = float(STATE.get("wallet_net_inr") or 0.0)
    cfg_override = int(_cfg_get("MAX_CONCURRENT_TRADES", 0) or 0)
    if cfg_override > 0:
        # User explicitly set a fixed value — respect it
        return max(1, cfg_override)
    if wallet <= 0:
        return 2
    if wallet < 15000:
        return 2
    if wallet < 30000:
        return 3
    if wallet < 60000:
        return 4
    return 5


def _regime_size_multiplier(side: str, regime: str, trend_direction: str) -> float:
    s = str(side or "BUY").upper()
    rg = str(regime or "UNKNOWN").upper()
    td = str(trend_direction or "UNKNOWN").upper()
    if s == "BUY":
        if rg in ("TRENDING", "TRENDING_UP") and td == "UP":
            # Regime-aligned long: go big — this is the best setup.
            return float(_cfg_get("SIZE_REGIME_ALIGNED_MULT", 1.25) or 1.25)
        if rg in ("VOLATILE", "UNKNOWN"):
            return float(_cfg_get("SIZE_REGIME_WEAK_MULT", 0.75) or 0.75)
        if rg == "SIDEWAYS":
            return float(_cfg_get("SIZE_REGIME_SIDEWAYS_MULT", 0.85) or 0.85)
    else:
        if rg in ("WEAK", "TRENDING_DOWN") and td == "DOWN":
            # Regime-aligned short: go big on confirmed downtrend.
            return float(_cfg_get("SIZE_REGIME_ALIGNED_MULT", 1.25) or 1.25)
        if rg in ("VOLATILE", "UNKNOWN"):
            return float(_cfg_get("SIZE_REGIME_WEAK_MULT", 0.75) or 0.75)
        if rg == "SIDEWAYS":
            return float(_cfg_get("SIZE_REGIME_SIDEWAYS_MULT", 0.80) or 0.80)
    return 1.0


def _calc_qty(symbol: str, price: float, tier: str = "FULL", tier_weight: float = 1.0, side: str = "BUY", regime: str = "UNKNOWN", trend_direction: str = "UNKNOWN", family: str = ""):
    wallet = float(STATE.get("wallet_net_inr") or 0.0)
    if wallet <= 0:
        wallet = float(getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
        append_log("WARNING", "BUCKET", "Wallet unavailable → fallback capital used")
    wallet_available = float(STATE.get("wallet_available_inr") or wallet)
    open_exposure = float(_current_exposure_inr())
    open_count = int(_open_positions_count())
    max_concurrent = _dynamic_max_concurrent()
    deployable_pct = max(1.0, min(100.0, float(_cfg_get("MAX_DEPLOYABLE_PCT", 75.0) or 75.0)))
    deployable_capital = wallet * (deployable_pct / 100.0)
    usable_capital = max(0.0, deployable_capital - open_exposure)
    exposure_remaining = max(0.0, _max_exposure_inr() - open_exposure)

    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)
    sig_seen = int(STATE.get("signals_seen_window") or 0)
    ent_done = int(STATE.get("entries_executed_window") or 0)
    tier_u = str(tier or "FULL").upper()
    tw = float(tier_weight or 1.0)
    slot_pressure = max(0.0, 1.0 - (open_count / float(max_concurrent)))
    if slot_pressure >= 0.75:
        # Aggressive: when most slots free, deploy 65% of usable capital per trade.
        # On a 10k wallet with 3 slots and 0 open: usable=7500, target=4875.
        opportunity_share = float(_cfg_get("OPPORTUNITY_SHARE_HIGH", 0.65) or 0.65)
    elif slot_pressure >= 0.50:
        opportunity_share = float(_cfg_get("OPPORTUNITY_SHARE_MED", 0.45) or 0.45)
    else:
        opportunity_share = float(_cfg_get("OPPORTUNITY_SHARE_LOW", 0.30) or 0.30)
    confidence_mult = max(0.2, tw)
    if tier_u == "FULL":
        # FULL tier gets 1.40x — reward high-conviction trades with bigger size.
        confidence_mult *= float(_cfg_get("SIZE_CONF_FULL_MULT", 1.40) or 1.40)
    elif tier_u == "REDUCED":
        confidence_mult *= float(_cfg_get("SIZE_CONF_REDUCED_MULT", 1.00) or 1.00)
    else:
        # MICRO = low conviction — tiny size, barely worth taking.
        # Better to skip than waste a slot on a weak setup.
        confidence_mult *= float(_cfg_get("SIZE_CONF_MICRO_MULT", 0.40) or 0.40)
    regime_mult = _regime_size_multiplier(side, regime, trend_direction)
    utilization = (open_exposure / deployable_capital) if deployable_capital > 0 else 0.0
    uplift = 1.0
    if tier_u == "FULL" and open_count <= 1 and utilization < float(_cfg_get("LOW_UTILIZATION_PCT", 0.35) or 0.35):
        uplift = min(float(_cfg_get("MAX_UTILIZATION_UPLIFT", 1.20) or 1.20), 1.0 + max(0, no_entry_cycles) * 0.03)
    opportunity_pressure_mult = max(0.70, min(1.30, (0.9 + slot_pressure * 0.4) * uplift))
    base_target_capital = usable_capital * max(0.05, min(0.75, opportunity_share))
    target_capital = base_target_capital * confidence_mult * regime_mult * opportunity_pressure_mult

    max_symbol_pct = max(1.0, min(100.0, float(_cfg_get("MAX_SYMBOL_ALLOCATION_PCT", 20.0) or 20.0)))
    symbol_cap = wallet * (max_symbol_pct / 100.0)
    affordability_cap = max(0.0, wallet_available)
    symbol_capital_cap = min(affordability_cap, usable_capital, symbol_cap, exposure_remaining)
    target_capital = max(0.0, min(target_capital, symbol_capital_cap))

    qty_from_capital = int(target_capital / price) if price > 0 else 0
    symbol_cap_qty = int(symbol_cap / price) if price > 0 else 0
    affordability_qty = int(affordability_cap / price) if price > 0 else 0
    stoploss_pct = max(0.01, float(_cfg_get("SHORT_STOPLOSS_PCT", 1.2) if str(side).upper() == "SELL" else _cfg_get("STOPLOSS_PCT", 2.0)) or 2.0)
    # Dynamic risk sizing: risk budget = RISK_PER_TRADE_PCT of wallet.
    # Default raised from 1.0% → 2.0% so risk_qty doesn't strangle capital_qty
    # on small accounts (10k wallet @ 1% = ₹100 risk budget = tiny positions).
    risk_per_trade_pct = max(0.01, float(_cfg_get("RISK_PER_TRADE_PCT", 2.0) or 2.0))
    risk_amount = wallet * (risk_per_trade_pct / 100.0)
    per_share_risk = price * (stoploss_pct / 100.0) if price > 0 else 0.0
    risk_qty = int(risk_amount / per_share_risk) if per_share_risk > 0 else 0

    # Blend capital_qty and risk_qty instead of strict min().
    # Use weighted average (70% capital, 30% risk) when capital_qty > risk_qty,
    # so risk acts as a drag, not a hard ceiling.  Still hard-capped at
    # symbol_cap_qty and affordability_qty for safety.
    cq = max(0, qty_from_capital)
    rq = max(0, risk_qty)
    if cq > rq and rq > 0:
        qty = int(rq * 0.30 + cq * 0.70)
    else:
        qty = min(cq, rq)
    reason_chain = [f"capital_qty={qty_from_capital}", f"risk_qty={risk_qty}", f"symbol_cap_qty={symbol_cap_qty}", f"affordability_qty={affordability_qty}"]
    short_policy_qty = qty
    if str(side or "BUY").upper() == "SELL":
        short_aligned = str(regime or "UNKNOWN").upper() in ("WEAK", "TRENDING_DOWN") and str(trend_direction or "UNKNOWN").upper() == "DOWN"
        short_mult = float(_cfg_get("SHORT_SIZE_ALIGNED_MULT", 1.0) if short_aligned else _cfg_get("SHORT_SIZE_NON_ALIGNED_MULT", _cfg_get("SHORT_SIZE_MULTIPLIER", 0.75)) or 0.75)
        short_policy_qty = int(math.floor(qty * max(0.1, short_mult)))
        qty = short_policy_qty
        reason_chain.append(f"short_policy_qty={short_policy_qty}")
    size_factor = float(STATE.get("reduce_size_factor") or 1.0)

    # Win-streak scaling applied BEFORE size_factor reduction so uplift acts on
    # the base qty, not on an already-penalised qty. Guard: only when no active
    # loss-driven reduction (size_factor==1.0 and loss_streak==0).
    loss_streak = int(STATE.get("loss_streak") or 0)
    regime_u_local = str(regime or "UNKNOWN").upper()
    if (
        loss_streak == 0
        and size_factor >= 1.0
        and tier_u == "FULL"
        and regime_u_local in ("TRENDING", "TRENDING_UP")
    ):
        recent_wins = int(STATE.get("consecutive_wins") or 0)
        if recent_wins >= 2:
            win_uplift = min(
                float(_cfg_get("WIN_STREAK_MAX_UPLIFT", 1.30) or 1.30),
                1.0 + 0.10 * min(recent_wins, 3),
            )
            qty = int(math.floor(qty * win_uplift))
            reason_chain.append(f"win_streak_uplift={win_uplift:.2f}(wins={recent_wins})")

    if size_factor < 1.0:
        qty = int(qty * max(0.1, size_factor))
        reason_chain.append(f"size_factor_qty={qty}")

    qty = qty if qty >= 1 else 0
    reason_chain.append(f"final_qty={qty}")
    open_positions = max(1, _open_positions_count())
    avg_position_size = (open_exposure / float(open_positions)) if open_positions > 0 else 0.0
    STATE["capital_utilization"] = {
        "deployable_capital": float(deployable_capital),
        "open_exposure": float(open_exposure),
        "unused_deployable": float(max(0.0, deployable_capital - open_exposure)),
        "utilization_pct": float((open_exposure / deployable_capital) * 100.0 if deployable_capital > 0 else 0.0),
        "avg_position_size": float(avg_position_size),
    }
    append_log(
        "INFO",
        "SIZE",
        f"[SIZE] symbol={symbol} side={side} family={family or '-'} tier={tier_u} regime={regime}/{trend_direction} "
        f"wallet_net={wallet:.2f} wallet_available={wallet_available:.2f} deployable_capital={deployable_capital:.2f} "
        f"usable_capital={usable_capital:.2f} exposure_remaining={exposure_remaining:.2f} base_target_capital={base_target_capital:.2f} "
        f"confidence_mult={confidence_mult:.2f} regime_mult={regime_mult:.2f} opportunity_pressure_mult={opportunity_pressure_mult:.2f} "
        f"target_capital={target_capital:.2f} risk_qty={risk_qty} capital_qty={qty_from_capital} symbol_cap_qty={symbol_cap_qty} "
        f"short_policy_qty={short_policy_qty} affordability_qty={affordability_qty} final_qty={qty} final_notional={(qty * price):.2f} "
        f"final_qty_reason_chain={'|'.join(reason_chain)}",
    )
    return qty, qty_from_capital, risk_qty


def _ltp(kite, sym):
    try:
        ins = f"{CFG.EXCHANGE}:{sym}"
        return float(kite.ltp([ins])[ins]["last_price"])
    except Exception:
        return None


def _validated_market_protection(value) -> float | None:
    try:
        mp = float(value)
    except Exception:
        return None
    if mp == -1.0 or (0.0 < mp <= 100.0):
        return mp
    return None


def _kite_supports_market_protection(kite) -> bool:
    global _KITE_MP_SUPPORT_CACHE
    cache_key = kite.__class__
    if cache_key in _KITE_MP_SUPPORT_CACHE:
        return bool(_KITE_MP_SUPPORT_CACHE.get(cache_key))
    supports = False
    try:
        sig = inspect.signature(getattr(kite, "place_order"))
        supports = "market_protection" in sig.parameters
        if not supports:
            supports = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    except Exception:
        supports = False
    _KITE_MP_SUPPORT_CACHE[cache_key] = bool(supports)
    return bool(supports)


def _kite_client_version() -> str:
    try:
        import kiteconnect  # type: ignore

        ver = getattr(kiteconnect, "__version__", None)
        if isinstance(ver, str) and ver:
            return ver
        if ver is not None and hasattr(ver, "__dict__"):
            return str(getattr(ver, "__version__", "") or "unknown")
    except Exception:
        pass
    return "unknown"


def _place_order_http_fallback(kite, params: dict):
    """Place an order via direct HTTP when the SDK's place_order() signature
    doesn't accept market_protection.  Uses a plain requests.Session rather
    than SDK internal attributes so it doesn't break when the SDK is updated.
    """
    import requests as _requests
    payload = {k: v for k, v in dict(params or {}).items() if v is not None}
    variety = str(payload.get("variety") or getattr(kite, "VARIETY_REGULAR", "regular")).lower()
    api_key = str(getattr(kite, "api_key", None) or CFG.KITE_API_KEY or "")
    access_token = str(getattr(kite, "access_token", None) or os.getenv("KITE_ACCESS_TOKEN") or CFG.KITE_ACCESS_TOKEN or "")
    if not api_key or not access_token:
        raise RuntimeError("http_fallback: api_key or access_token missing")
    url = f"https://api.kite.trade/orders/{variety}"
    headers = {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }
    resp = _requests.post(url, data=payload, headers=headers, timeout=7)
    data = resp.json()
    if int(resp.status_code) == 200 and isinstance(data, dict):
        return ((data.get("data") or {}).get("order_id")) or data.get("order_id")
    raise RuntimeError(f"http_fallback_failed status={resp.status_code} body={str(data)[:200]}")


def place_order_safe(kite, **kwargs):
    params = dict(kwargs or {})
    order_type = str(params.get("order_type") or "").upper()
    if order_type in (str(getattr(kite, "ORDER_TYPE_MARKET", "MARKET")).upper(), str(getattr(kite, "ORDER_TYPE_SLM", "SL-M")).upper()):
        mp = _validated_market_protection(params.get("market_protection"))
        if mp is None:
            raise ValueError(f"invalid market_protection={params.get('market_protection')}")
        params["market_protection"] = mp
    if _kite_supports_market_protection(kite):
        return kite.place_order(**params)
    return _place_order_http_fallback(kite, params)


# ============================================================================
# Trading mode + risk profile helpers (INTRADAY/SWING/HYBRID, STANDARD/GOD)
# ============================================================================

_VALID_MODES = ("INTRADAY", "SWING", "HYBRID")
_VALID_PROFILES = ("STANDARD", "GOD")


def _normalize_trading_mode(m) -> str:
    m = str(m or "").strip().upper()
    if m in ("MIS",):
        return "INTRADAY"
    if m in ("CNC", "DELIVERY"):
        return "SWING"
    return m if m in _VALID_MODES else "INTRADAY"


def _normalize_risk_profile(p) -> str:
    p = str(p or "").strip().upper()
    return p if p in _VALID_PROFILES else "STANDARD"


def current_trading_mode() -> str:
    return _normalize_trading_mode(STATE.get("trading_mode"))


def current_risk_profile() -> str:
    return _normalize_risk_profile(STATE.get("risk_profile"))


def effective_cfg(key: str):
    """Return the effective config value, respecting GOD profile overrides.

    GOD activates GOD_<KEY> overrides if defined. GOD NEVER bypasses:
    wallet_available, broker margin/affordability, order safety / market
    protection / compliance, panic, daily loss kill switch.
    """
    profile = current_risk_profile()
    if profile == "GOD":
        god_key = f"GOD_{key}"
        if hasattr(CFG, god_key):
            return getattr(CFG, god_key)
    return getattr(CFG, key, None)


def classify_trade_mode(signal_ctx: dict | None = None) -> tuple[str, str]:
    """Classify an intended entry as INTRADAY or SWING.

    Returns (trade_mode, reason). trade_mode ∈ {"INTRADAY","SWING"}.

    INTRADAY mode  → every entry is INTRADAY (current behaviour).
    SWING mode     → long entries become SWING; shorts stay INTRADAY.
    HYBRID mode    → narrow swing lane for strong long continuation:
        side in (BUY, LONG), strategy_tag == 'mtf_confirmed_long',
        tier == 'FULL', regime ∈ (TRENDING_UP, TRENDING),
        trend_direction == 'UP', not a weak-market/fallback/MR exception.
    All shorts are always INTRADAY.
    """
    ctx = dict(signal_ctx or {})
    mode = current_trading_mode()
    side = str(ctx.get("side") or "").upper()

    if side not in ("BUY", "LONG"):
        return "INTRADAY", "short_always_intraday"

    if mode == "INTRADAY":
        return "INTRADAY", "mode_intraday"

    if mode == "SWING":
        return "SWING", "mode_swing_long"

    if mode == "HYBRID":
        tag = str(ctx.get("strategy_tag") or "").lower()
        family = str(ctx.get("strategy_family") or "").lower()
        tier = str(ctx.get("tier") or "").upper()
        regime = str(ctx.get("regime") or "").upper()
        trend_direction = str(ctx.get("trend_direction") or "").upper()
        weak_exception = bool(ctx.get("weak_market_exception"))

        if weak_exception:
            return "INTRADAY", "weak_market_exception_not_swing_eligible"
        if "mean_reversion" in family or "mean_reversion" in tag:
            return "INTRADAY", "mean_reversion_not_swing_eligible"
        if "fallback" in family or "fallback" in tag:
            return "INTRADAY", "fallback_not_swing_eligible"
        if tag != "mtf_confirmed_long":
            return "INTRADAY", f"strategy_tag_not_mtf_confirmed_long({tag or 'none'})"
        if tier != "FULL":
            return "INTRADAY", f"tier_not_full({tier or 'none'})"
        if regime not in ("TRENDING_UP", "TRENDING"):
            return "INTRADAY", f"regime_not_trending({regime or 'none'})"
        if trend_direction != "UP":
            return "INTRADAY", f"trend_direction_not_up({trend_direction or 'none'})"
        return "SWING", "hybrid_qualified_long_continuation"

    return "INTRADAY", "fallback_intraday"


def product_for_trade_mode(trade_mode: str) -> str:
    """Map a trade_mode to its broker product. SWING → CNC, else MIS."""
    return "CNC" if str(trade_mode or "").upper() == "SWING" else "MIS"


def set_trading_mode(new_mode: str) -> tuple[bool, str]:
    """Update STATE['trading_mode'] + persist to env. Returns (ok, normalized)."""
    norm = _normalize_trading_mode(new_mode)
    if norm not in _VALID_MODES:
        return False, norm
    with STATE_LOCK:
        STATE["trading_mode"] = norm
    try:
        from env_utils import set_env_value as _sev
        _sev("TRADING_MODE", norm)
        os.environ["TRADING_MODE"] = norm
    except Exception as exc:
        append_log("WARN", "MODE", f"trading_mode_env_write_failed: {exc}")
    append_log("INFO", "MODE", f"trading_mode_set mode={norm}")
    return True, norm


def set_risk_profile(new_profile: str) -> tuple[bool, str]:
    """Switch runtime risk profile. Does NOT perform GOD confirmation — caller
    must confirm separately via confirm_god_mode() when switching to GOD.
    """
    norm = _normalize_risk_profile(new_profile)
    if norm not in _VALID_PROFILES:
        return False, norm
    with STATE_LOCK:
        STATE["risk_profile"] = norm
        STATE["pending_risk_profile_confirmation"] = None
    try:
        from env_utils import set_env_value as _sev
        _sev("RISK_PROFILE", norm)
        os.environ["RISK_PROFILE"] = norm
    except Exception as exc:
        append_log("WARN", "RISK", f"risk_profile_env_write_failed: {exc}")
    append_log("INFO", "RISK", f"risk_profile_set profile={norm}")
    return True, norm


def request_god_confirmation() -> str:
    """Stage a pending GOD activation. Returns a warning string for the UI."""
    with STATE_LOCK:
        STATE["pending_risk_profile_confirmation"] = "GOD"
    append_log("INFO", "RISK", "god_confirmation_requested")
    return (
        "⚠️ GOD MODE — CONFIRM TO ACTIVATE\n\n"
        "GOD removes bot-imposed soft caps (deployable %, max exposure %, "
        "per-symbol allocation, tier weights, weak-market multipliers).\n\n"
        "GOD does NOT bypass: wallet_available, broker margin / CNC "
        "affordability, market protection / compliance, panic, or the daily "
        "loss kill switch.\n\n"
        "Risks:\n"
        "• Higher exposure per trade and overall\n"
        "• Larger drawdown potential\n"
        "• More aggressive wallet usage\n\n"
        "To activate, reply: `/riskprofile god confirm`\n"
        "To cancel, reply: `/riskprofile cancel`"
    )


def confirm_god_mode() -> tuple[bool, str]:
    """Finalize a pending GOD activation. Fails if no pending confirmation."""
    with STATE_LOCK:
        pending = STATE.get("pending_risk_profile_confirmation")
    if str(pending or "").upper() != "GOD":
        return False, "no_pending_god_confirmation"
    ok, norm = set_risk_profile("GOD")
    if ok:
        append_log("INFO", "RISK", "god_confirmation_accepted")
    return ok, norm


def cancel_god_confirmation() -> bool:
    """Clear any pending GOD confirmation without changing the active profile."""
    with STATE_LOCK:
        had = STATE.get("pending_risk_profile_confirmation")
        STATE["pending_risk_profile_confirmation"] = None
    if had:
        append_log("INFO", "RISK", "god_confirmation_cancelled")
    return bool(had)


def _get_product_for_mode(product_override: str | None = None) -> str:
    """Return order product type based on trading mode.

    SWING mode   → CNC (delivery, zero brokerage, hold overnight).
    INTRADAY     → MIS (margin intraday, auto-squared off by broker at 3:20).
    HYBRID       → MIS by default; callers must pass an explicit
                   `product_override` for per-trade swing routing. This keeps
                   recon/UI paths that call _get_product_for_mode() without a
                   trade context safely defaulting to intraday.
    """
    if product_override:
        return product_override
    mode = current_trading_mode()
    if mode == "SWING":
        return "CNC"
    return str(CFG.PRODUCT or "MIS")


def _place_live_order(kite, sym, side, qty, product_override: str | None = None):
    evaluate_ip_compliance(force=False)
    if not bool(STATE.get("live_order_allowed")):
        append_log("ERROR", "ORDER", f"Order blocked {sym} {side} qty={qty}: ip_non_compliant_or_not_rearmed")
        return None

    market_protection = _validated_market_protection(_cfg_get("MARKET_PROTECTION", 0.2))
    if market_protection is None:
        append_log("ERROR", "ORDER", f"Order blocked {sym} {side} qty={qty}: market_protection_invalid")
        return None

    product = _get_product_for_mode(product_override)
    retries = 3
    backoff = 0.25
    try:
        for i in range(retries):
            try:
                _order_rate_limit_wait()
                order_type = kite.ORDER_TYPE_MARKET
                kwargs = dict(
                    variety=kite.VARIETY_REGULAR,
                    exchange=CFG.EXCHANGE,
                    tradingsymbol=sym,
                    transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    product=product,
                )
                kwargs["order_type"] = order_type
                slm_const = getattr(kite, "ORDER_TYPE_SLM", "SL-M")
                if order_type in (kite.ORDER_TYPE_MARKET, slm_const):
                    kwargs["market_protection"] = market_protection
                append_log("INFO", "ORDER", f"Placing {side} {qty}x {sym} product={product}")
                return place_order_safe(kite, **kwargs)
            except Exception as e:
                msg = str(e)
                if "429" in msg and i < (retries - 1):
                    append_log("WARN", "ORDER", f"429 retry {i+1}/{retries} for {sym} {side}")
                    time.sleep(backoff * (2 ** i))
                    continue
                raise
    except Exception as e:
        append_log("ERROR", "ORDER", f"Order failed {sym} {side} qty={qty} product={product}: {e}")
        return None


def _wait_for_fill(kite, order_id: str, fallback_price: float, timeout: float = 6.0) -> float | None:
    """Poll order history until COMPLETE or timeout, returning the average fill price.

    Returns ``None`` when the order was REJECTED or CANCELLED so the caller can
    abort position creation instead of tracking a phantom trade.  Falls back to
    *fallback_price* only on timeout (order still pending — likely to fill).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            history = kite.order_history(order_id)
            if history:
                last = history[-1]
                status = str(last.get("status") or "").upper()
                if status == "COMPLETE":
                    avg = float(last.get("average_price") or 0.0)
                    filled_qty = int(last.get("filled_quantity") or 0)
                    if avg > 0 and filled_qty > 0:
                        return avg
                elif status in ("REJECTED", "CANCELLED"):
                    append_log(
                        "ERROR", "FILL",
                        f"order_id={order_id} status={status} — aborting position creation",
                    )
                    return None
        except Exception as exc:
            append_log("WARN", "FILL", f"order_history_poll_failed order_id={order_id}: {exc}")
        time.sleep(0.5)
    append_log(
        "WARN", "FILL",
        f"fill_timeout order_id={order_id} after {timeout:.0f}s — using pre-order LTP {fallback_price:.2f}",
    )
    return fallback_price


def _set_cooldown():
    # Tier-based cooldowns: FULL=45s, REDUCED=75s, MICRO=90s (flat 120s was leaving
    # capital idle between fast momentum setups on FULL-tier entries).
    tier = str(STATE.get("entry_tier_for_cooldown") or "").upper()
    if tier == "FULL":
        sec = int(_cfg_get("COOLDOWN_FULL_SECONDS", 45))
    elif tier == "REDUCED":
        sec = int(_cfg_get("COOLDOWN_REDUCED_SECONDS", 75))
    else:
        sec = int(_cfg_get("COOLDOWN_SECONDS", 90))
    now = datetime.now(IST)
    post_0930 = now.time() >= dt_time(9, 30)
    if post_0930 and tier in ("MICRO", "REDUCED"):
        streak_key = "post0930_valid_tier_streak"
        streak = safe_update(STATE, streak_key, lambda v: int(v or 0) + 1)
        if streak >= 2:
            sec = max(30, int(round(sec * 0.75)))
            append_log("INFO", "COOLDOWN", f"[COOLDOWN] refinement applied tier={tier} streak={streak} sec={sec}")
    else:
        safe_set(STATE, "post0930_valid_tier_streak", 0)
    with STATE_LOCK:
        STATE["cooldown_until"] = datetime.now(IST) + timedelta(seconds=sec)
        STATE["entry_tier_for_cooldown"] = None


def _apply_skip_cooldown(sym: str, reason: str, minutes: int = 3, side: str = "BUY", signal_price: float | None = None, strategy_tag: str = ""):
    sym = (sym or "").strip().upper()
    if not sym:
        return
    post_0930 = datetime.now(IST).time() >= dt_time(9, 30)
    refined_minutes = int(minutes)
    if post_0930 and reason in ("qty_zero", "opening_filter_low_confidence", "risk_guard"):
        refined_minutes = max(1, int(round(refined_minutes * 0.67)))
        append_log("INFO", "COOLDOWN", f"[COOLDOWN] refinement applied symbol={sym} reason={reason} minutes={refined_minutes}")
    until = datetime.now(IST) + timedelta(minutes=max(1, refined_minutes))
    STATE.setdefault("skip_cooldown", {})[sym] = until
    append_log("INFO", "SKIP", f"{sym} cooldown applied reason={reason}")
    try:
        rec = {"symbol": sym, "side": str(side or "BUY").upper(), "reason": reason}
        if strategy_tag:
            rec["strategy_tag"] = strategy_tag
        if signal_price is not None:
            rec["signal_price"] = float(signal_price)
        SA.record_skipped_signal(rec)
    except Exception:
        pass


def _skip_cooldown_active(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    until = STATE.setdefault("skip_cooldown", {}).get(sym)
    if not until:
        return False
    if datetime.now(IST) >= until:
        STATE["skip_cooldown"].pop(sym, None)
        return False
    return True


def _close_position(sym, reason="MANUAL", ltp_override=None):
    sym = (sym or "").strip().upper()
    trade = _positions().get(sym)
    if not trade:
        return False
    entry, qty = _trade_entry_qty(trade)
    side = str(trade.get("side") or "LONG").upper()
    ltp = float(ltp_override) if ltp_override is not None else None

    if not is_live_enabled():
        if ltp is None:
            ltp = entry
        pnl, pnl_pct = _calc_pnl(entry, ltp, qty, side=side)
        safe_update(STATE, "today_pnl", lambda x: float(x or 0.0) + pnl)
        RISK.update_loss_streak(STATE, pnl)
        RISK.check_day_drawdown_guard(STATE)
        PM.remove(sym)
        STATE["last_exit_ts"][sym] = datetime.now(IST)
        _set_cooldown()
        exit_time = datetime.now(IST).isoformat(timespec="seconds")
        enriched = dict(trade)
        enriched.update({"symbol": sym, "qty": qty, "entry": entry, "exit_price": ltp, "exit_time": exit_time, "exit_reason": reason})
        _log_trade_event("CLOSE", enriched)
        _log_trade_event("TRADE", {**enriched, "pnl_inr": pnl, "pnl_pct": pnl_pct})
        append_log("WARN", "EXIT", f"{sym} family={trade.get('strategy_family','-')} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
        append_log("INFO", "RISK", f"symbol={sym} loss_streak={int(STATE.get('loss_streak') or 0)} halt_for_day={bool(STATE.get('halt_for_day'))}")
        _append_runtime_event("recent_exits", {"symbol": sym, "side": side, "qty": qty, "entry": entry, "exit": ltp, "reason": reason, "pnl_inr": pnl, "ts": exit_time}, limit=40)
        SA.record_trade_exit(
            {
                "entry_time": trade.get("entry_time") or "",
                "symbol": sym,
                "side": side,
                "qty": qty,
                "entry": entry,
                "exit": ltp,
                "pnl_inr": pnl,
                "pnl_pct": pnl_pct,
                "reason": reason,
                "strategy_tag": trade.get("strategy_tag") or "unknown",
                "strategy_family": trade.get("strategy_family") or "unknown",
                "market_regime": trade.get("market_regime") or "UNKNOWN",
                "universe_source": trade.get("universe_source") or "primary",
                "sector": trade.get("sector") or _sector_for_symbol(sym),
            }
        )
        exit_side = "BUY" if side == "SHORT" else "SELL"
        _notify(f"🔴 {exit_side} PAPER\nSymbol: {sym}\nExit: {ltp:.2f}\nPnL ₹: {pnl:.2f}\nPnL %: {pnl_pct:.2f}%\nReason: {reason}")
        return True

    kite = get_kite()
    oid = None
    close_side = "BUY" if side == "SHORT" else "SELL"
    # Close with the same product type the trade was opened with.
    trade_product = str(trade.get("product") or _get_product_for_mode())
    for _ in range(3):
        oid = _place_live_order(kite, sym, close_side, qty, product_override=trade_product)
        if oid:
            break
        time.sleep(0.6)
    if not oid:
        return False
    if ltp is None:
        ltp = _ltp(kite, sym) or entry

    pnl, pnl_pct = _calc_pnl(entry, ltp, qty, side=side)
    safe_update(STATE, "today_pnl", lambda x: float(x or 0.0) + pnl)
    RISK.update_loss_streak(STATE, pnl)
    RISK.check_day_drawdown_guard(STATE)
    PM.remove(sym)
    STATE["last_exit_ts"][sym] = datetime.now(IST)
    _set_cooldown()
    exit_time = datetime.now(IST).isoformat(timespec="seconds")
    enriched = dict(trade)
    enriched.update({"symbol": sym, "qty": qty, "entry": entry, "exit_price": ltp, "exit_time": exit_time, "exit_reason": reason})
    if oid:
        enriched["order_id"] = oid
    _log_trade_event("CLOSE", enriched)
    _log_trade_event("TRADE", {**enriched, "pnl_inr": pnl, "pnl_pct": pnl_pct})
    append_log("WARN", "EXIT", f"{sym} family={trade.get('strategy_family','-')} reason={reason} pnl_inr={pnl:.2f} pnl_pct={pnl_pct:.2f}%")
    append_log("INFO", "RISK", f"symbol={sym} loss_streak={int(STATE.get('loss_streak') or 0)} halt_for_day={bool(STATE.get('halt_for_day'))}")
    _append_runtime_event("recent_exits", {"symbol": sym, "side": side, "qty": qty, "entry": entry, "exit": ltp, "reason": reason, "pnl_inr": pnl, "ts": exit_time}, limit=40)
    SA.record_trade_exit(
        {
            "entry_time": trade.get("entry_time") or "",
            "symbol": sym,
            "side": side,
            "qty": qty,
            "entry": entry,
            "exit": ltp,
            "pnl_inr": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "strategy_tag": trade.get("strategy_tag") or "unknown",
            "strategy_family": trade.get("strategy_family") or "unknown",
            "market_regime": trade.get("market_regime") or "UNKNOWN",
            "universe_source": trade.get("universe_source") or "primary",
            "sector": trade.get("sector") or _sector_for_symbol(sym),
        }
    )
    _notify(f"🔴 {close_side} LIVE\nSymbol: {sym}\nExit: {ltp:.2f}\nPnL ₹: {pnl:.2f}\nPnL %: {pnl_pct:.2f}%\nReason: {reason}")
    return True


def _apply_strategy_allocation(
    qty: int,
    strategy_tag: str,
    *,
    tier: str = "",
    side: str = "",
    regime: str = "",
    trend_direction: str = "",
) -> int:
    pre_qty = max(0, int(qty))
    mult, reason = SA.get_strategy_multiplier(strategy_tag, CFG)
    post_qty = max(0, int(math.floor(pre_qty * max(0.0, mult))))

    tier_u = str(tier or "").upper()
    side_u = str(side or "").upper()
    regime_u = str(regime or "").upper()
    trend_u = str(trend_direction or "").upper()
    mtf_tag_ok = strategy_tag in ("mtf_confirmed_long", "mtf_confirmed_short")
    tier_ok = tier_u == "FULL" and tier_u != "MICRO"
    is_long_side = side_u in ("BUY", "LONG")
    is_short_side = side_u in ("SELL", "SHORT")
    if is_long_side:
        regime_aligned = (regime_u in ("TRENDING_UP", "TRENDING")) and trend_u == "UP"
    elif is_short_side:
        regime_aligned = (regime_u in ("WEAK", "TRENDING_DOWN")) and trend_u == "DOWN"
    else:
        regime_aligned = False
    full_floor_override = bool(
        reason == "low_expectancy"
        and pre_qty >= 1
        and post_qty <= 0
        and mtf_tag_ok
        and tier_ok
        and regime_aligned
    )
    reduced_sideways_flat_override = bool(
        reason == "low_expectancy"
        and pre_qty >= 1
        and post_qty <= 0
        and strategy_tag == "mtf_confirmed_long"
        and tier_u == "REDUCED"
        and tier_u != "MICRO"
        and side_u in ("BUY", "LONG")
        and regime_u == "SIDEWAYS"
        and trend_u == "FLAT"
    )
    floor_override = bool(full_floor_override or reduced_sideways_flat_override)
    if floor_override:
        post_qty = 1

    append_log(
        "INFO",
        "ALLOC",
        f"strategy={strategy_tag} tier={tier_u or '-'} side={side_u or '-'} regime={regime_u or '-'} trend_direction={trend_u or '-'} "
        f"pre_allocation_qty={pre_qty} allocation_multiplier={mult:.2f} "
        f"allocation_reason={reason} post_allocation_qty={post_qty} allocation_floor_override={(1 if floor_override else 0)}",
    )
    return post_qty


def _close_all_open_trades(reason="MANUAL"):
    return ee_force_exit_all(_positions(), _close_position, reason=reason)


def _within_entry_window():
    now = datetime.now(IST)
    mode = str(STATE.get("trading_mode") or "INTRADAY").upper()
    if mode == "SWING":
        # Swing/CNC: wider entry window — can buy anytime during market hours.
        # Skip the volatile open (09:15-09:20), allow entries until 15:15.
        sh, sm = _parse_hhmm(os.getenv("SWING_ENTRY_START", "09:20"))
        eh, em = _parse_hhmm(os.getenv("SWING_ENTRY_END", "15:15"))
    else:
        sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
        eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def _can_open_new_trade(sym, entry, qty=1, momentum_positive=False):
    sym = sym.strip().upper()
    now = datetime.now(IST)

    with STATE_LOCK:
        cooldown_until = STATE.get("cooldown_until")
        last_exit = STATE.get("last_exit_ts", {}).get(sym)
        halt_for_day = bool(STATE.get("halt_for_day"))
        pause_until = STATE.get("pause_entries_until")
        wallet_avail = float(STATE.get("wallet_available_inr") or 0.0)
        wallet_net = float(STATE.get("wallet_net_inr") or 0.0)

    if cooldown_until and now < cooldown_until:
        append_log("INFO", "SKIP", f"{sym} reason=cooldown")
        return False

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    reentry_block = int(getattr(CFG, "REENTRY_BLOCK_MINUTES", 30))
    if last_exit and (now - last_exit) < timedelta(minutes=reentry_block):
        if not momentum_positive:
            append_log("INFO", "SKIP", f"{sym} reason=reentry_block")
            return False
        append_log("INFO", "SKIP", f"{sym} reentry_block bypassed reason=positive_momentum")

    if halt_for_day:
        append_log("INFO", "SKIP", f"{sym} reason=halt_for_day")
        return False

    if pause_until and now < pause_until:
        append_log("INFO", "SKIP", f"{sym} reason=pause_entries")
        return False

    if sym in _positions():
        append_log("INFO", "SKIP", f"{sym} reason=already_held")
        return False

    max_pos = _dynamic_max_concurrent()
    if _open_positions_count() >= max_pos:
        append_log("INFO", "SKIP", f"{sym} reason=max_positions")
        return False

    required_value = float(entry) * max(1, int(qty or 1))
    if required_value > wallet_avail:
        append_log("INFO", "SKIP", f"{sym} reason=insufficient_wallet need={required_value:.2f} avail={wallet_avail:.2f}")
        return False

    if not RISK.can_enter_trade(sym, float(entry), _positions(), wallet_net, int(qty), sector=_sector_for_symbol(sym)):
        _apply_skip_cooldown(sym, "risk_guard")
        return False

    return True




def _compute_symbol_momentum_pct(sym: str) -> float:
    try:
        token = token_for_symbol(sym)
        kite = get_kite()
        data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=2), pd.Timestamp.now(), "15minute")
        df = pd.DataFrame(data)
        if df.empty or "close" not in df.columns or len(df) < 2:
            return 0.0
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2])
        if prev <= 0:
            return 0.0
        return ((last - prev) / prev) * 100.0
    except Exception:
        return 0.0


def _active_trade_universe() -> list:
    dyn = [str(x).strip().upper() for x in (STATE.get("research_universe") or []) if str(x).strip()]
    excl = set(load_excluded())
    out, seen = [], set()
    for s in dyn:
        if s in excl or s in seen:
            continue
        seen.add(s)
        out.append(s)
    try:
        limit = int(getattr(CFG, "RESEARCH_UNIVERSE_SIZE", 20) or 20)
        if limit > 0:
            out = out[:limit]
    except Exception:
        pass
    return out


def _strategy_registry_map() -> dict:
    return {str(x.get("family") or "").strip().lower(): dict(x) for x in STRATEGY_REGISTRY}


def _recent_family_performance_score(family: str) -> float:
    fam = str(family or "").strip().lower()
    if not fam:
        return 50.0
    try:
        rows = SA._read_csv_rows(SA.TRADE_HISTORY_PATH)  # type: ignore[attr-defined]
    except Exception:
        rows = []
    if not rows:
        return 50.0
    hist = [r for r in rows if str(r.get("strategy_family") or "").strip().lower() == fam][-20:]
    if not hist:
        return 50.0
    pnl = [float(r.get("pnl_inr") or 0.0) for r in hist]
    wins = [x for x in pnl if x > 0]
    win_rate = (len(wins) / len(pnl)) if pnl else 0.0
    recent = sum(pnl[-8:]) if pnl else 0.0
    score = (50.0 * win_rate) + (50.0 if recent > 0 else (30.0 if recent == 0 else 10.0))
    return max(0.0, min(100.0, score))


def _estimate_family_opportunity_count(family: str, symbols: list[str], sample_size: int = 14) -> int:
    fam = str(family or "").strip().lower()
    syms = list(dict.fromkeys([(s or "").strip().upper() for s in symbols if (s or "").strip()]))[: max(4, sample_size)]
    if not syms:
        return 0
    count = 0
    for sym in syms:
        q = _quality_metrics(sym)
        if not q.get("ok"):
            continue
        price = float(q.get("price") or 0.0)
        sma = float(q.get("sma20") or 0.0)
        sma_prev = float(q.get("sma20_prev") or 0.0)
        vol = float(q.get("vol_score") or 0.0)
        if price <= 0 or sma <= 0:
            continue
        dist_pct = ((price - sma) / sma) * 100.0
        if fam in ("trend_long", "fallback_long"):
            if price > sma and vol >= 1.0:
                count += 1
        elif fam == "pullback_long":
            if price >= (sma * 0.998) and sma >= sma_prev and vol >= 0.9:
                count += 1
        elif fam == "mean_reversion":
            if abs(dist_pct) <= 0.8 and vol >= 0.8:
                count += 1
        elif fam == "outlier_long":
            if price > (sma * 1.003) and vol >= 1.2:
                count += 1
        elif fam in ("short_breakdown", "fallback_short"):
            if price < sma and vol >= 1.0:
                count += 1
        elif fam == "outlier_short":
            if price < (sma * 0.997) and vol >= 1.2:
                count += 1
    return int(count)


def score_strategy_family(family: str, market_context: dict, universe_context: dict, recent_stats: dict | None = None) -> tuple[int, dict]:
    fam = str(family or "").strip().lower()
    registry = _strategy_registry_map().get(fam, {})
    regime = str(market_context.get("regime") or "UNKNOWN").upper()
    trend_direction = str(market_context.get("trend_direction") or "UNKNOWN").upper()
    opening_mode = str(market_context.get("opening_mode") or "OPEN_CLEAN").upper()
    volatility = str(market_context.get("volatility_state") or ("HIGH" if regime == "VOLATILE" else "NORMAL")).upper()
    quality = float(market_context.get("quality_score") or 50.0)
    sector_bias = float(universe_context.get("sector_bias") or 0.0)
    opp_count = int(universe_context.get("opportunity_count") or 0)
    htf_bias = float(market_context.get("htf_bias") or 0.0)
    recent_perf = float((recent_stats or {}).get("recent_perf_score") or _recent_family_performance_score(fam))

    preferred_regimes = [str(x).upper() for x in list(registry.get("preferred_regimes") or [])]
    preferred_struct = [str(x).upper() for x in list(registry.get("preferred_market_structure") or [])]
    direction = str(registry.get("direction") or "either").lower()

    regime_fit = 30.0 if regime in preferred_regimes else (20.0 if regime == "UNKNOWN" else 8.0)
    directional_fit = 12.0
    if direction == "long":
        directional_fit = 20.0 if trend_direction == "UP" else (10.0 if trend_direction == "UNKNOWN" else 4.0)
    elif direction == "short":
        directional_fit = 20.0 if trend_direction == "DOWN" else (10.0 if trend_direction == "UNKNOWN" else 4.0)
    volatility_fit = 8.0
    if regime == "VOLATILE":
        volatility_fit = 10.0 if fam in ("outlier_long", "outlier_short", "fallback_short") else 4.0
    elif volatility == "LOW":
        volatility_fit = 10.0 if fam in ("trend_long", "pullback_long", "mean_reversion") else 6.0

    opportunity_fit = min(15.0, float(opp_count) * 3.0)
    sector_fit = 10.0 if ((sector_bias >= 0 and direction != "short") or (sector_bias < 0 and direction == "short")) else 5.0
    htf_fit = 10.0 if ((htf_bias >= 0 and direction != "short") or (htf_bias < 0 and direction == "short")) else 4.0
    perf_fit = max(0.0, min(5.0, recent_perf / 20.0))

    if opening_mode not in preferred_struct:
        regime_fit = max(2.0, regime_fit - 6.0)
    if quality < 40 and fam in ("trend_long", "pullback_long"):
        volatility_fit = max(1.0, volatility_fit - 3.0)
    if regime in ("WEAK", "TRENDING_DOWN") and direction == "short":
        directional_fit = min(20.0, directional_fit + 3.0)
    if regime == "TRENDING_UP" and direction == "long":
        directional_fit = min(20.0, directional_fit + 2.0)

    total = regime_fit + directional_fit + volatility_fit + opportunity_fit + sector_fit + htf_fit + perf_fit
    score = int(round(max(0.0, min(100.0, total))))
    detail = {
        "regime_fit": round(regime_fit, 2),
        "directional_fit": round(directional_fit, 2),
        "volatility_fit": round(volatility_fit, 2),
        "opportunity_fit": round(opportunity_fit, 2),
        "sector_fit": round(sector_fit, 2),
        "htf_fit": round(htf_fit, 2),
        "performance_fit": round(perf_fit, 2),
        "opportunity_count": opp_count,
    }
    return score, detail


def _refresh_active_strategy_families(reason: str, regime: str, trend_direction: str, active_universe: list, research_universe: list) -> list[str]:
    prev_active = list(STATE.get("active_strategy_families") or [])
    market_context = {
        "regime": str(regime or "UNKNOWN").upper(),
        "trend_direction": str(trend_direction or "UNKNOWN").upper(),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN").upper(),
        "quality_score": float(get_opening_confidence(STATE.get("opening_metrics") or {})[0] if STATE.get("opening_metrics") else 60.0),
        "volatility_state": "HIGH" if str(regime).upper() == "VOLATILE" else "NORMAL",
        "htf_bias": 1.0 if str(trend_direction).upper() == "UP" else (-1.0 if str(trend_direction).upper() == "DOWN" else 0.0),
    }
    sector_snapshot = _sector_strength_snapshot(active_universe[:10] if active_universe else research_universe[:10])
    sector_bias = (sum(sector_snapshot.values()) / len(sector_snapshot)) if sector_snapshot else 0.0
    source_symbols = active_universe or research_universe or []

    scored = []
    score_map = {}
    append_log("INFO", "ROUTE", f"[ROUTE] recomputing top3 strategies reason={reason}")
    for st in STRATEGY_REGISTRY:
        fam = str(st.get("family") or "").strip().lower()
        opp_n = _estimate_family_opportunity_count(fam, source_symbols)
        universe_context = {"sector_bias": sector_bias, "opportunity_count": opp_n}
        score, detail = score_strategy_family(fam, market_context, universe_context, recent_stats=None)
        append_log("INFO", "ROUTE", f"[ROUTE] strategy_score family={fam} score={score}")
        score_map[fam] = {"score": score, "detail": detail}
        scored.append((fam, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    min_active = int(getattr(CFG, "STRATEGY_MIN_ACTIVE_SCORE", 40) or 40)
    active = [fam for fam, sc in scored if sc >= min_active][:3]
    STATE["active_strategy_families"] = list(active)
    STATE["active_strategy_last_refresh"] = float(time.time())
    STATE["active_strategy_last_reason"] = str(reason or "timer")
    STATE["strategy_scores_last"] = score_map
    hist = list(STATE.get("strategy_selection_history") or [])
    hist.append(
        {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "reason": str(reason or "timer"),
            "regime": str(regime or "UNKNOWN"),
            "top3": list(active),
        }
    )
    STATE["strategy_selection_history"] = hist[-200:]
    append_log("INFO", "ROUTE", f"[ROUTE] active_top3={','.join(active) if active else 'none'}")
    if tuple(prev_active) != tuple(active):
        _append_runtime_event(
            "route_changes_today",
            {
                "reason": str(reason or "timer"),
                "from_top3": prev_active,
                "to_top3": list(active),
                "regime": str(regime or "UNKNOWN"),
                "trend_direction": str(trend_direction or "UNKNOWN"),
            },
            limit=240,
        )
        _record_research_event("top3_change", f"reason={reason}", from_top3=prev_active, to_top3=list(active))
    return active


def _maybe_refresh_active_strategy_families(regime: str, trend_direction: str, active_universe: list, research_universe: list) -> list[str]:
    last = float(STATE.get("active_strategy_last_refresh") or 0.0)
    refresh_min = int(getattr(CFG, "STRATEGY_SELECTION_REFRESH_MINUTES", 10) or 10)
    elapsed_min = ((time.time() - last) / 60.0) if last > 0 else 999.0
    prev_regime = str(STATE.get("strategy_last_regime") or "")
    cur_regime = str(regime or "UNKNOWN").upper()
    dry_n = int(STATE.get("top3_dry_cycles") or 0)
    dry_thr = int(getattr(CFG, "TOP3_DRY_CYCLE_THRESHOLD", 5) or 5)
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)

    reason = ""
    if not list(STATE.get("active_strategy_families") or []):
        reason = "init"
    elif prev_regime and prev_regime != cur_regime:
        reason = "regime_change"
    elif elapsed_min >= max(1, refresh_min):
        reason = "timer"
    elif bool(STATE.get("fallback_mode_active")):
        reason = "fallback_mode"
    elif dry_n >= dry_thr:
        reason = "top3_dry"
    elif no_entry_cycles >= max(3, dry_thr):
        reason = "no_entry_cycles"

    STATE["strategy_last_regime"] = cur_regime
    if reason:
        return _refresh_active_strategy_families(reason, cur_regime, trend_direction, active_universe, research_universe)
    return list(STATE.get("active_strategy_families") or [])


def _scan_family(family: str, universe: list, max_new: int, universe_source: str) -> int:
    fam = str(family or "").strip().lower()
    if max_new <= 0:
        return 0
    if fam == "trend_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "pullback_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_pullback_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "mean_reversion":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_mean_reversion_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "fallback_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_mean_reversion_signal, strategy_family=fam, universe_source=universe_source)
    if fam == "outlier_long":
        return _scan_long_entries(universe, max_new=max_new, signal_fn=generate_vwap_ema_signal, strategy_family=fam, universe_source=universe_source)
    if fam in ("short_breakdown", "fallback_short", "outlier_short"):
        if not bool(getattr(CFG, "ENABLE_SHORT_MODE", True)):
            return 0
        return _scan_short_entries(universe, max_new=max_new, strategy_family=fam, universe_source=universe_source)
    return 0


def _scan_top3_families(universe: list, families: list[str], max_new: int, universe_source: str) -> int:
    if not universe or max_new <= 0:
        return 0
    fams = [str(f).strip().lower() for f in families if str(f).strip()]
    prev_source = str(STATE.get("last_route_universe_source") or "n/a")
    STATE["last_route_universe_source"] = str(universe_source or "n/a")
    if prev_source != STATE["last_route_universe_source"]:
        _append_runtime_event(
            "route_changes_today",
            {"reason": "universe_source_change", "from_source": prev_source, "to_source": STATE["last_route_universe_source"], "families": list(fams)},
            limit=240,
        )
        _record_research_event("route_change", f"source={prev_source}->{STATE['last_route_universe_source']}", active_top3=list(fams))
        _record_universe_change(
            "route_driven_universe_change",
            STATE["last_route_universe_source"],
            [],
            [],
            fallback_active=bool(STATE.get("fallback_mode_active")),
        )
    append_log("INFO", "ROUTE", f"[ROUTE] scanning families={','.join(fams) if fams else 'none'} source={universe_source}")
    opened = 0
    for fam in fams:
        if opened >= max_new:
            break
        opened += _scan_family(fam, universe, max_new=max_new - opened, universe_source=universe_source)
    return opened

def _load_research_universe_from_file() -> list:
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = set(load_excluded())
    syms = [s for s in syms if s not in excl]
    if syms:
        STATE["research_universe"] = list(syms)
        append_log("INFO", "UNIV", f"Loaded from file size={len(syms)}")
    return syms


def _resolve_trade_universe() -> list:
    if isinstance(getattr(RE, "research_state", {}).get("last_report"), dict):
        STATE["research_last_report"] = dict(getattr(RE, "research_state", {}).get("last_report"))
    dyn = _active_trade_universe()
    if dyn:
        append_log("INFO", "UNIV", f"Research universe loaded size={len(dyn)}")
        _record_research_event("night_or_live_universe", f"source=runtime size={len(dyn)}")
        return dyn

    from_file = _load_research_universe_from_file()
    if from_file:
        append_log("INFO", "UNIV", f"Research universe loaded size={len(from_file)}")
        _record_research_event("night_or_live_universe", f"source=file size={len(from_file)}")
        return from_file

    fallback = load_universe_trading()
    if fallback:
        append_log("INFO", "UNIV", f"Fallback static size={len(fallback)}")
        _record_research_event("fallback_activation", f"source=trading_file size={len(fallback)}")
    return fallback


def _record_research_event(event_type: str, message: str, **extra):
    payload = {"event": event_type, "message": message}
    if extra:
        payload.update(extra)
    _append_runtime_event("research_events", payload, limit=400)
    append_log("INFO", "RESEARCH", f"[RESEARCH] {event_type} {message}")


def _record_universe_change(reason: str, source: str, added: list[str], removed: list[str], fallback_active: bool = False):
    rec = {
        "reason": reason,
        "source": source,
        "added": list(added or []),
        "removed": list(removed or []),
        "fallback_active": bool(fallback_active),
    }
    _append_runtime_event("universe_changes_today", rec, limit=240)
    append_log(
        "INFO",
        "UNIV",
        f"[UNIV_CHANGE] reason={reason} source={source} added={','.join(added or []) or '-'} removed={','.join(removed or []) or '-'} fallback={bool(fallback_active)}",
    )
    _record_research_event("universe_change", f"reason={reason} source={source}", added=added, removed=removed, fallback_active=bool(fallback_active))


def _sector_strength_snapshot(research_universe: list) -> dict:
    bucket = {}
    for sym in research_universe or []:
        sec = str(SECTOR_MAP.get(str(sym).upper(), "OTHER") or "OTHER").upper()
        mom = _compute_symbol_momentum_pct(str(sym).upper())
        bucket.setdefault(sec, []).append(float(mom))
    out = {}
    for sec, arr in bucket.items():
        out[sec] = (sum(arr) / len(arr)) if arr else 0.0
    return out


def _active_score_metrics(symbol: str, sector_strength: dict) -> dict:
    try:
        df = _htf_fetch(symbol, days=4)
        if df.empty or "close" not in df.columns or "volume" not in df.columns:
            return {"ok": False}
        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        if len(close) < 8:
            return {"ok": False}

        intraday_change = ((float(close.iloc[-1]) - float(close.iloc[-4])) / float(close.iloc[-4]) * 100.0) if float(close.iloc[-4]) > 0 else 0.0
        rel_vol = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0
        momentum = ((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100.0) if float(close.iloc[-2]) > 0 else 0.0
        vwap = (close * vol).sum() / max(1.0, float(vol.sum()))
        vwap_distance = ((float(close.iloc[-1]) - float(vwap)) / float(vwap) * 100.0) if float(vwap) > 0 else 0.0
        sec = str(SECTOR_MAP.get(symbol, "OTHER") or "OTHER").upper()
        sec_strength = float(sector_strength.get(sec, 0.0))

        score = (0.35 * intraday_change) + (0.25 * rel_vol) + (0.20 * momentum) + (0.10 * vwap_distance) + (0.10 * sec_strength)
        return {"ok": True, "score": float(score)}
    except Exception:
        return {"ok": False}


def build_active_universe(research_universe: list) -> list:
    base = [str(s).strip().upper() for s in (research_universe or []) if str(s).strip()]
    prev = list(STATE.get("active_universe") or [])
    if not base:
        STATE["active_universe"] = []
        if prev:
            _record_universe_change("active_universe_empty", "active_universe", [], prev, fallback_active=bool(STATE.get("fallback_mode_active")))
        return []
    n = int(getattr(CFG, "ACTIVE_UNIVERSE_SIZE", 8) or 8)
    sec_strength = _sector_strength_snapshot(base)
    scored = []
    for sym in base:
        m = _active_score_metrics(sym, sec_strength)
        if m.get("ok"):
            scored.append((sym, float(m.get("score") or 0.0)))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in scored[: max(1, n)]]
    if not out:
        out = base[: max(1, n)]
    STATE["active_universe"] = out
    STATE["active_universe_last_refresh"] = datetime.now(IST)
    append_log("INFO", "UNIV", f"Active universe refreshed size={len(out)}")
    prev_set, out_set = set(prev), set(out)
    added = sorted(list(out_set - prev_set))
    removed = sorted(list(prev_set - out_set))
    if added or removed:
        _record_universe_change("active_universe_refresh", "active_universe", added, removed, fallback_active=bool(STATE.get("fallback_mode_active")))
    return out


def refresh_active_universe_if_due(research_universe: list):
    mins = int(getattr(CFG, "ACTIVE_UNIVERSE_REFRESH_MINUTES", 10) or 10)
    last = STATE.get("active_universe_last_refresh")
    if not STATE.get("active_universe"):
        return build_active_universe(research_universe)
    if not isinstance(last, datetime):
        return build_active_universe(research_universe)
    if datetime.now(IST) - last >= timedelta(minutes=max(1, mins)):
        return build_active_universe(research_universe)
    return list(STATE.get("active_universe") or [])


def _passes_sector_entry_filter(sym: str) -> bool:
    """Explicit sector filter step before risk checks (separate from exposure guard)."""
    sec = str(SECTOR_MAP.get(sym, "OTHER") or "OTHER").upper()
    limit = int(getattr(CFG, "SECTOR_MAX_IN_UNIVERSE", 3) or 3)
    held = 0
    for hs, tr in _positions().items():
        hsec = str((tr or {}).get("sector") or SECTOR_MAP.get(str(hs).upper(), "OTHER") or "OTHER").upper()
        if hsec == sec:
            held += 1
    if held >= limit:
        append_log("INFO", "SECTOR", f"{sym} sector={sec} cap reached ({held}/{limit}) -> skip")
        return False
    return True



def get_research_rank(symbol: str, research_universe: list) -> int:
    sym = (symbol or "").strip().upper()
    if not sym:
        return -1
    for idx, s in enumerate(research_universe or [], start=1):
        if str(s).strip().upper() == sym:
            return idx
    return -1


def is_top_ranked_symbol(symbol: str, research_universe: list, top_n: int) -> bool:
    rank = get_research_rank(symbol, research_universe)
    return rank > 0 and rank <= max(1, int(top_n or 1))


def _research_score_for_symbol(symbol: str):
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    reports = []
    if isinstance(STATE.get("research_last_report"), dict):
        reports.append(STATE.get("research_last_report"))
    if isinstance(getattr(RE, "research_state", {}).get("last_report"), dict):
        reports.append(getattr(RE, "research_state", {}).get("last_report"))

    for report in reports:
        for r in list(report.get("top_ranked") or []):
            if str((r or {}).get("symbol") or "").strip().upper() == sym:
                try:
                    return float(r.get("final_score"))
                except Exception:
                    continue
    return None


def _weak_market_quality_metrics(symbol: str) -> dict:
    try:
        token = token_for_symbol(symbol)
        kite = get_kite()
        data = kite.historical_data(token, pd.Timestamp.now() - pd.Timedelta(days=45), pd.Timestamp.now(), "day")
        df = pd.DataFrame(data)
        if df.empty or "close" not in df.columns or "volume" not in df.columns or len(df) < 22:
            return {"ok": False, "reason": "weak_momentum"}

        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        sma20 = close.rolling(20).mean()
        if len(sma20.dropna()) < 2:
            return {"ok": False, "reason": "weak_momentum"}

        price = float(close.iloc[-1])
        sma_curr = float(sma20.iloc[-1])
        sma_prev = float(sma20.iloc[-2])
        vol_score = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0

        price_gt_sma = price > sma_curr
        slope_pos = sma_curr > sma_prev
        vol_ok = vol_score > float(getattr(CFG, "WEAK_MARKET_MIN_VOLUME_SCORE", 1.0) or 1.0)

        if not (price_gt_sma and slope_pos and vol_ok):
            return {
                "ok": False,
                "reason": "weak_momentum",
                "price_gt_sma20": price_gt_sma,
                "sma20_slope_pos": slope_pos,
                "volume_score": vol_score,
            }

        return {
            "ok": True,
            "price_gt_sma20": price_gt_sma,
            "sma20_slope_pos": slope_pos,
            "volume_score": vol_score,
        }
    except Exception:
        return {"ok": False, "reason": "weak_momentum"}


def passes_weak_market_filter(symbol: str, research_universe: list) -> tuple[bool, str, dict]:
    sym = (symbol or "").strip().upper()
    # Raised top_n from 10 → 20 and lowered min_score from 0.90 → 0.75.
    # 0.90 was blocking top-25% setups in weak markets; now top-50% qualify.
    top_n = int(getattr(CFG, "WEAK_MARKET_TOP_N", 20) or 20)
    if not is_top_ranked_symbol(sym, research_universe, top_n):
        return False, "not_top_ranked", {}

    score = _research_score_for_symbol(sym)
    min_score = float(getattr(CFG, "WEAK_MARKET_MIN_SCORE", 0.75) or 0.75)
    if score is None or score < min_score:
        return False, "score_too_low", {"score": score}

    q = _weak_market_quality_metrics(sym)
    if not q.get("ok"):
        return False, str(q.get("reason") or "weak_momentum"), q

    meta = {"rank": get_research_rank(sym, research_universe), "score": score}
    meta.update(q)
    return True, "allowed", meta


def is_market_entry_allowed(symbol: str, regime: str, research_universe: list) -> tuple[bool, str, dict]:
    rg = str(regime or "UNKNOWN").upper()
    if rg in ("WEAK", "TRENDING_DOWN"):
        return passes_weak_market_filter(symbol, research_universe)
    return True, "allowed", {}



def _open_short_positions_count() -> int:
    c = 0
    for _s, tr in _positions().items():
        if str((tr or {}).get("side") or "LONG").upper() == "SHORT":
            c += 1
    return c


def _calc_pnl(entry: float, ltp: float, qty: int, side: str = "LONG") -> tuple[float, float]:
    side_u = str(side or "LONG").upper()
    if side_u == "SHORT":
        pnl_inr = (entry - ltp) * qty
        pnl_pct = ((entry - ltp) / entry * 100.0) if entry > 0 else 0.0
    else:
        pnl_inr = (ltp - entry) * qty
        pnl_pct = ((ltp - entry) / entry * 100.0) if entry > 0 else 0.0
    return float(pnl_inr), float(pnl_pct)


def _htf_fetch(symbol: str, days: int = 20) -> pd.DataFrame:
    try:
        token = token_for_symbol(symbol)
        data = get_kite().historical_data(
            token,
            pd.Timestamp.now() - pd.Timedelta(days=days),
            pd.Timestamp.now(),
            str(_cfg_get("HTF_INTERVAL", "15m") or "15m"),
        )
        return pd.DataFrame(data)
    except Exception:
        return pd.DataFrame()


def _kite_fetch_custom(symbol: str, days: int, interval: str) -> pd.DataFrame:
    """Fetch historical data for *symbol* at the specified *interval*.

    Returns a DataFrame with Title-cased columns (Open/High/Low/Close/Volume)
    and a tz-aware DatetimeIndex in IST, or an empty DataFrame on failure.
    """
    try:
        token = token_for_symbol(symbol)
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=days)
        data = get_kite().historical_data(token, from_dt, to_dt, interval)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        rename = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "date" in df.columns:
            idx = pd.to_datetime(df["date"])
            if idx.dt.tz is None:
                idx = idx.dt.tz_localize("Asia/Kolkata")
            else:
                idx = idx.dt.tz_convert("Asia/Kolkata")
            df.index = idx
            df = df.drop(columns=["date"])
        return df
    except Exception:
        return pd.DataFrame()


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    c = close.astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return (100.0 - (100.0 / (1.0 + rs))).astype(float).fillna(50.0)


def _nifty_reference_return(days: int = 45, bars: int = 20) -> float | None:
    ref_sym = str(getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES") or "NIFTYBEES").strip().upper()
    ref_df = _htf_fetch(ref_sym, days=days)
    if ref_df.empty or "close" not in ref_df.columns:
        return None
    ref_close = ref_df["close"].astype(float)
    if len(ref_close) <= bars:
        return None
    base = float(ref_close.iloc[-(bars + 1)])
    last = float(ref_close.iloc[-1])
    if base <= 0:
        return None
    return ((last - base) / base) * 100.0


def _session_bucket(now: datetime | None = None) -> str:
    t = (now or datetime.now(IST)).time()
    if t >= datetime.strptime("09:15", "%H:%M").time() and t < datetime.strptime("09:30", "%H:%M").time():
        return "EARLY"
    if t >= datetime.strptime("09:30", "%H:%M").time() and t < datetime.strptime("13:30", "%H:%M").time():
        return "MAIN"
    if t >= datetime.strptime("13:30", "%H:%M").time() and t <= datetime.strptime("15:00", "%H:%M").time():
        return "LATE"
    return "OFF"


def _in_open_filter_window(now: datetime | None = None) -> bool:
    t = (now or datetime.now(IST)).time()
    sh, sm = _parse_hhmm(getattr(CFG, "OPEN_FILTER_START", "09:15"))
    eh, em = _parse_hhmm(getattr(CFG, "OPEN_FILTER_END", "09:30"))
    start = datetime.now(IST).replace(hour=sh, minute=sm, second=0, microsecond=0).time()
    end = datetime.now(IST).replace(hour=eh, minute=em, second=0, microsecond=0).time()
    return start <= t <= end


def _compute_opening_metrics() -> dict:
    out = {
        "gap_pct": 0.0,
        "first_5m_range_pct": 0.0,
        "direction_clear": False,
        "spread_quality": "UNKNOWN",
        "volume_quality": "UNKNOWN",
        "valid": False,
        "data_state": "INCOMPLETE",
        "feed_error": False,
        "feed_exception": "",
    }
    stability_sym = str(getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES") or "NIFTYBEES").strip().upper()
    try:
        # Daily candles — need at least yesterday + today to compute gap
        d1 = _kite_fetch_custom(stability_sym, days=10, interval="day")
        # 5-minute candles — need today's intraday data
        m5 = _kite_fetch_custom(stability_sym, days=2, interval="5minute")

        if d1 is None or d1.empty or m5 is None or m5.empty:
            return out
        d1 = d1.dropna(subset=["Close"])
        m5 = m5.dropna(subset=["Close"])
        if len(d1) < 2 or len(m5) < 3:
            return out

        d1_close = pd.to_numeric(d1["Close"], errors="coerce")
        d1_open = pd.to_numeric(d1["Open"], errors="coerce") if "Open" in d1.columns else d1_close
        prev_close = float(d1_close.iloc[-2])
        open_today = float(d1_open.iloc[-1])
        gap_pct = ((open_today - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

        # first intraday candles of the current trading day
        today = datetime.now(IST).date()
        day_df = m5[m5.index.date == today]
        if day_df.empty:
            return out

        first = day_df.iloc[0]
        o5 = float(pd.to_numeric(first.get("Open", 0.0), errors="coerce"))
        h5 = float(pd.to_numeric(first.get("High", 0.0), errors="coerce"))
        l5 = float(pd.to_numeric(first.get("Low", 0.0), errors="coerce"))
        close_ser = pd.to_numeric(day_df["Close"], errors="coerce")
        c_last = float(close_ser.iloc[-1])
        ma3 = float(close_ser.rolling(3).mean().dropna().iloc[-1]) if len(close_ser) >= 3 and not close_ser.dropna().empty else c_last
        first_5m_range_pct = ((h5 - l5) / o5 * 100.0) if o5 > 0 else 0.0
        direction_clear = abs((c_last - o5) / o5 * 100.0) >= 0.15 and (
            (c_last > ma3 and c_last > o5) or (c_last < ma3 and c_last < o5)
        )

        vol = pd.to_numeric(day_df["Volume"], errors="coerce") if "Volume" in day_df.columns else pd.Series(dtype=float)
        volume_quality = "GOOD"
        if not vol.empty and len(vol) > 1:
            v0 = float(vol.iloc[0])
            vm = float(vol.tail(min(20, len(vol))).mean())
            volume_quality = "GOOD" if vm <= 0 or (v0 / vm) >= 0.8 else "LOW"

        max_range = float(getattr(CFG, "MAX_SAFE_FIRST_5M_RANGE_PCT", 1.2) or 1.2)
        spread_quality = "GOOD" if first_5m_range_pct <= max_range * 1.4 else "WIDE"

        out.update(
            {
                "gap_pct": float(gap_pct),
                "first_5m_range_pct": float(first_5m_range_pct),
                "direction_clear": bool(direction_clear),
                "spread_quality": spread_quality,
                "volume_quality": volume_quality,
                "valid": True,
                "data_state": "READY",
                "feed_error": False,
            }
        )
        return out
    except Exception as e:
        out["data_state"] = "FEED_ERROR"
        out["feed_error"] = True
        out["feed_exception"] = str(e)
        return out


def get_opening_confidence(metrics: dict | None = None) -> tuple[int, dict]:
    m = dict(metrics or _compute_opening_metrics() or {})
    if bool(m.get("feed_error")):
        return 0, {"considered": [], "ignored": [], "data_state": "FEED_ERROR", "feed_error": True, "decision_path": "feed_error"}
    gap = abs(float(m.get("gap_pct") or 0.0))
    rng = float(m.get("first_5m_range_pct") or 0.0)
    max_gap = float(getattr(CFG, "MAX_SAFE_GAP_PCT", 0.8) or 0.8)
    max_rng = float(getattr(CFG, "MAX_SAFE_FIRST_5M_RANGE_PCT", 1.2) or 1.2)
    dir_ok = bool(m.get("direction_clear"))
    spread_q = str(m.get("spread_quality") or "UNKNOWN").upper()
    volume_q = str(m.get("volume_quality") or "UNKNOWN").upper()

    parts = []
    considered = []

    if max_gap > 0:
        gscore = max(0.0, min(100.0, 100.0 * (1.0 - (gap / (max_gap * 1.5)))))
        parts.append(gscore)
        considered.append("gap")

    if rng > 0 and max_rng > 0:
        rscore = max(0.0, min(100.0, 100.0 * (1.0 - (rng / (max_rng * 1.6)))))
        parts.append(rscore)
        considered.append("range")

    if bool(m.get("valid")):
        parts.append(100.0 if dir_ok else 30.0)
        considered.append("trend")

    if volume_q != "UNKNOWN":
        parts.append(100.0 if volume_q == "GOOD" else 25.0)
        considered.append("volume")

    if spread_q != "UNKNOWN":
        parts.append(100.0 if spread_q == "GOOD" else 20.0)
        considered.append("spread")

    ignored = []
    if volume_q == "UNKNOWN":
        ignored.append("volume")
    if spread_q == "UNKNOWN":
        ignored.append("spread")
    if rng <= 0:
        ignored.append("opening_range")

    score = int(round(sum(parts) / len(parts))) if parts else 55
    if ignored and score >= 100:
        # Incomplete opening inputs must never present as full confidence.
        score = 69
    score = max(0, min(100, score))
    meta = {
        "considered": considered,
        "ignored": ignored,
        "data_state": str(m.get("data_state") or "INCOMPLETE"),
        "feed_error": bool(m.get("feed_error")),
        "decision_path": "incomplete_data" if ignored else "complete_data",
    }
    return score, meta


def get_opening_mode() -> tuple[str, dict]:
    if not bool(getattr(CFG, "USE_ADAPTIVE_OPEN_FILTER", True)):
        return "OPEN_CLEAN", {"valid": False}
    if not _in_open_filter_window():
        return "OPEN_CLEAN", {"valid": False}

    m = _compute_opening_metrics()
    gap = abs(float(m.get("gap_pct") or 0.0))
    max_gap = float(getattr(CFG, "MAX_SAFE_GAP_PCT", 0.8) or 0.8)
    spread_q = str(m.get("spread_quality") or "UNKNOWN").upper()
    volume_q = str(m.get("volume_quality") or "UNKNOWN").upper()

    conf, conf_meta = get_opening_confidence(m)
    m["confidence"] = conf
    m["confidence_meta"] = conf_meta

    append_log("INFO", "OPEN", f"gap_pct={float(m.get('gap_pct') or 0.0):.2f}")
    append_log("INFO", "OPEN", f"first_5m_range_pct={float(m.get('first_5m_range_pct') or 0.0):.2f}")
    append_log("INFO", "OPEN", f"spread_quality={spread_q}")
    append_log("INFO", "OPEN", f"volume_quality={volume_q}")
    if bool(m.get("feed_error")):
        retries = safe_update(STATE, "open_feed_retry_count", lambda v: int(v or 0) + 1)
        hard_after = int(_cfg_get("OPEN_FEED_HARD_BLOCK_RETRIES", 3) or 3)
        m["decision_path"] = "feed_error"
        if retries >= max(2, hard_after):
            m["reason"] = "confirmed_broken_feed"
            m["data_state"] = "FEED_ERROR_CONFIRMED"
            append_log("WARN", "OPEN", f"decision_path={m.get('decision_path')} data_state={m.get('data_state')} feed_error=True exception={m.get('feed_exception') or ''}")
            return "OPEN_HARD_BLOCK", m
        m["reason"] = "transient_feed_error_soft"
        m["data_state"] = "FEED_ERROR_RETRYING"
        append_log("WARN", "OPEN", f"decision_path={m.get('decision_path')} data_state={m.get('data_state')} feed_error=True exception={m.get('feed_exception') or ''}")
        return "OPEN_MODERATE", m
    # Missing/unknown opening metrics (volume/spread/opening_range) must remain
    # an incomplete-data state and never escalate to confirmed_broken_feed.
    safe_set(STATE, "open_feed_retry_count", 0)

    if gap > max_gap * 1.5:
        m["reason"] = "confirmed_extreme_gap"
        m["decision_path"] = "extreme_gap"
        return "OPEN_HARD_BLOCK", m

    if not bool(m.get("valid")):
        m["reason"] = "incomplete_opening_data"
        m["decision_path"] = "incomplete_data"
        m["data_state"] = "INCOMPLETE"
        if conf_meta.get("ignored"):
            append_log("INFO", "OPEN", f"missing data ignored: {','.join(conf_meta.get('ignored') or [])}")
        append_log("INFO", "OPEN", f"decision_path={m.get('decision_path')} data_state={m.get('data_state')} feed_error=False exception=")
        return "OPEN_MODERATE", m

    if spread_q == "UNKNOWN" or volume_q == "UNKNOWN":
        m["reason"] = "incomplete_opening_data"
        m["decision_path"] = "incomplete_data"
        m["data_state"] = "INCOMPLETE"
        if conf_meta.get("ignored"):
            append_log("INFO", "OPEN", f"missing data ignored: {','.join(conf_meta.get('ignored') or [])}")
        append_log("INFO", "OPEN", f"decision_path={m.get('decision_path')} data_state={m.get('data_state')} feed_error=False exception=")
        return "OPEN_MODERATE", m

    now = datetime.now(IST)
    pre_0930 = now.time() < dt_time(9, 30)
    if conf < 40:
        if pre_0930 and ("opening_range" in (conf_meta.get("ignored") or []) or spread_q == "UNKNOWN" or volume_q == "UNKNOWN"):
            m["reason"] = "pre0930_incomplete_data_softened"
            m["decision_path"] = "pre0930_incomplete_data"
            append_log("INFO", "OPEN", "pre-09:30 reduced entry allowed due to incomplete opening data")
            append_log("INFO", "OPEN", f"decision_path={m.get('decision_path')} data_state={m.get('data_state') or 'INCOMPLETE'} feed_error=False exception=")
            return "OPEN_MODERATE", m
        m["reason"] = "unstable_open"
        m["decision_path"] = "unstable_open"
        return "OPEN_UNSAFE", m
    if conf < 70:
        m["reason"] = "incomplete_opening_data"
        m["decision_path"] = "incomplete_data"
        if conf_meta.get("ignored"):
            append_log("INFO", "OPEN", f"missing data ignored: {','.join(conf_meta.get('ignored') or [])}")
        return "OPEN_MODERATE", m
    m["reason"] = "opening_conditions_clean"
    m["decision_path"] = "clean_open"
    return "OPEN_CLEAN", m


def _htf_volume_surge_score(symbol: str) -> float:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return 0.0
    return float(q.get("vol_score") or 0.0)


def confirm_long_htf(symbol: str, regime: str | None = None) -> bool:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return True
    rg = str(regime or (get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN").upper()
    htf_status = _htf_alignment_status(symbol, "BUY", rg)
    htf_score = 2 if htf_status == "PASS" else (1 if htf_status == "PARTIAL" else (1 if htf_status == "INSUFFICIENT_HISTORY" else 0))

    if rg in ("TRENDING", "TRENDING_UP"):
        ok = htf_score >= 2
    elif rg == "SIDEWAYS":
        ok = htf_score >= 1
    elif rg == "VOLATILE":
        vol_req = float(getattr(CFG, "VOLATILE_HTF_MIN_VOL_SCORE", 1.2) or 1.2)
        vol_score = _htf_volume_surge_score(symbol)
        ok = (htf_score >= 2) and (vol_score >= vol_req)
        if not ok:
            append_log(
                "INFO",
                "CONFIRM",
                f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score} vol_score={vol_score:.2f} vol_req={vol_req:.2f}",
            )
            return False
    else:
        ok = htf_score >= 1

    if ok and bool(getattr(CFG, "HTF_CONFIRM_RSI", False)):
        df = _htf_fetch(symbol)
        if df.empty or "close" not in df.columns:
            ok = False
        else:
            close = df["close"].astype(float)
            rsi = _calc_rsi(close)
            min_rsi = float(getattr(CFG, "HTF_LONG_MIN_RSI", 52.0) or 52.0)
            rv = float(rsi.iloc[-1]) if len(rsi) > 0 and pd.notna(rsi.iloc[-1]) else 0.0
            ok = rv >= min_rsi
            if not ok:
                append_log(
                    "INFO",
                    "CONFIRM",
                    f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score} rsi={rv:.2f} rsi_min={min_rsi:.2f}",
                )
                return False

    if not ok:
        append_log(
            "INFO",
            "CONFIRM",
            f"BUY blocked {symbol} reason=HTF_Score_Below_Req_for_{rg} htf_status={htf_status} htf_score={htf_score}",
        )
    return ok


def confirm_short_htf(symbol: str) -> bool:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return True
    df = _htf_fetch(symbol)
    if df.empty or "close" not in df.columns:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
        return False
    ma_n = int(getattr(CFG, "HTF_CONFIRM_MA", 20) or 20)
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean().dropna()
    if len(ma) < 2:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
        return False
    ok = float(close.iloc[-1]) < float(ma.iloc[-1]) and float(ma.iloc[-1]) < float(ma.iloc[-2])
    if ok and bool(getattr(CFG, "HTF_CONFIRM_RSI", False)):
        rsi = _calc_rsi(close)
        max_rsi = float(getattr(CFG, "HTF_SHORT_MAX_RSI", 48.0) or 48.0)
        ok = float(rsi.iloc[-1]) <= max_rsi
    if not ok:
        append_log("INFO", "CONFIRM", f"SHORT blocked {symbol} reason=htf_not_bearish")
    return ok


def _quality_metrics(symbol: str) -> dict:
    df = _htf_fetch(symbol, days=45)
    if df.empty or "close" not in df.columns or "volume" not in df.columns or len(df) < 22:
        return {"ok": False}
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    sma20 = close.rolling(20).mean().dropna()
    if len(sma20) < 2:
        return {"ok": False}
    vol_score = float(vol.iloc[-1]) / float(vol.tail(20).mean()) if float(vol.tail(20).mean()) > 0 else 0.0
    ret_20d = ((float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100.0) if float(close.iloc[-21]) > 0 else 0.0
    nifty_20d = _nifty_reference_return(days=45, bars=20)
    rs_vs_nifty = (ret_20d - nifty_20d) if nifty_20d is not None else None
    return {
        "ok": True,
        "price": float(close.iloc[-1]),
        "sma20": float(sma20.iloc[-1]),
        "sma20_prev": float(sma20.iloc[-2]),
        "vol_score": vol_score,
        "ret_20d": ret_20d,
        "rs_vs_nifty": rs_vs_nifty,
    }


def _opening_symbol_quality_ok(symbol: str, side: str = "BUY") -> bool:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return False
    price = float(q.get("price") or 0.0)
    sma20 = float(q.get("sma20") or 0.0)
    vol_score = float(q.get("vol_score") or 0.0)
    side_u = str(side or "BUY").upper()
    if side_u == "SHORT":
        return price < sma20 and vol_score >= float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2)
    return price > sma20 and vol_score >= 1.0


def _opening_size_multiplier() -> float:
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if mode == "OPEN_HARD_BLOCK":
        return 0.0
    if mode == "OPEN_UNSAFE":
        return float(getattr(CFG, "OPEN_UNSAFE_SIZE_MULTIPLIER", 0.25) or 0.25)
    if mode == "OPEN_MODERATE":
        return float(getattr(CFG, "OPEN_MODERATE_SIZE_MULTIPLIER", 0.5) or 0.5)
    if mode == "OPEN_CLEAN":
        return float(getattr(CFG, "OPEN_CLEAN_SIZE_MULTIPLIER", 1.0) or 1.0)
    return 1.0

def _opening_selective_entry_allowed(symbol: str, side: str = "BUY") -> tuple[bool, str]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return False, "invalid_symbol"
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    research_universe = _active_trade_universe()
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)
    fallback_cycles = int(getattr(CFG, "OPEN_MIN_TRADE_AFTER_NO_EXEC_CYCLES", 8) or 8)

    if mode == "OPEN_HARD_BLOCK":
        return False, "hard_block_opening"

    if mode == "OPEN_UNSAFE":
        top_n = int(getattr(CFG, "OPEN_UNSAFE_TOP_N", 5) or 5)
        min_score = float(getattr(CFG, "OPEN_UNSAFE_MIN_SCORE", 0.90) or 0.90)
        if research_universe and not is_top_ranked_symbol(sym, research_universe, top_n):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        score = _research_score_for_symbol(sym)
        if score is not None and score < min_score:
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        if not _opening_symbol_quality_ok(sym, side=side):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        return True, "allowed"

    if mode == "OPEN_MODERATE":
        top_n = int(getattr(CFG, "OPEN_MODERATE_TOP_N", 10) or 10)
        if research_universe and not is_top_ranked_symbol(sym, research_universe, top_n):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        if not _opening_symbol_quality_ok(sym, side=side):
            return (True, "fallback_min_trade") if no_entry_cycles >= fallback_cycles else (False, "opening_filter_low_confidence")
        return True, "allowed"

    return True, "allowed"


def _session_quality_score() -> float:
    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if mode == "OPEN_HARD_BLOCK":
        return 0.2
    if mode == "OPEN_UNSAFE":
        return 0.3
    if mode == "OPEN_MODERATE":
        return 0.6
    return 1.0


def _htf_alignment_status(symbol: str, side: str, regime: str) -> str:
    if not bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        return "PASS"
    df = _htf_fetch(symbol)
    if df.empty or "close" not in df.columns:
        return "INSUFFICIENT_HISTORY"
    ma_n = int(getattr(CFG, "HTF_CONFIRM_MA", 20) or 20)
    close = df["close"].astype(float)
    ma = close.rolling(ma_n).mean().dropna()
    if len(ma) < 2:
        return "INSUFFICIENT_HISTORY"

    px = float(close.iloc[-1])
    ma_now = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-2])
    bucket = _session_bucket()
    strictness = str(STATE.get("confirm_strictness") or "STRICT").upper()
    rg = str(regime or "UNKNOWN").upper()
    side_u = str(side or "BUY").upper()

    if side_u == "SHORT":
        partial = px < ma_now
        full = partial and ma_now < ma_prev
        if rg in ("WEAK", "TRENDING_DOWN"):
            return "PASS" if full else ("PARTIAL" if partial else "FAIL")
        if bucket == "EARLY" or strictness == "MODERATE":
            return "PARTIAL" if partial else "FAIL"
        return "PASS" if full else ("PARTIAL" if partial else "FAIL")

    partial = px > ma_now
    full = partial and ma_now > ma_prev
    if rg in ("TRENDING", "TRENDING_UP"):
        if strictness == "STRICT" and bucket != "EARLY":
            return "PASS" if full else ("PARTIAL" if partial else "FAIL")
        return "PARTIAL" if partial else "FAIL"
    if rg == "SIDEWAYS":
        return "PARTIAL" if partial else "FAIL"
    if rg == "VOLATILE":
        vol_req = float(getattr(CFG, "VOLATILE_HTF_MIN_VOL_SCORE", 1.2) or 1.2)
        q = _quality_metrics(symbol)
        vol_score = float(q.get("vol_score") or 0.0) if q.get("ok") else 0.0
        if full and vol_score >= vol_req:
            return "PASS"
        return "FAIL"
    if rg in ("WEAK", "TRENDING_DOWN"):
        q = _quality_metrics(symbol)
        exceptional = bool(q.get("ok")) and float(q.get("vol_score") or 0.0) >= 1.3 and float(q.get("price") or 0.0) > float(q.get("sma20") or 0.0)
        if full and exceptional:
            return "PASS"
        return "PARTIAL" if partial and exceptional else "FAIL"

    if bucket == "EARLY" or strictness == "MODERATE":
        return "PARTIAL" if partial else "FAIL"
    return "PASS" if full else ("PARTIAL" if partial else "FAIL")


def _entry_tier_from_score(score: int) -> str:
    if score >= int(_cfg_get("ENTRY_FULL_MIN_SCORE", 80) or 80):
        return "FULL"
    if score >= int(_cfg_get("ENTRY_REDUCED_MIN_SCORE", 60) or 60):
        return "REDUCED"
    if score >= int(_cfg_get("ENTRY_MICRO_MIN_SCORE", 45) or 45):
        return "MICRO"
    return "BLOCK"


def _entry_tier_multiplier(tier: str) -> float:
    t = str(tier or "BLOCK").upper()
    if t == "FULL":
        return float(_cfg_get("FULL_TIER_WEIGHT", 1.25) or 1.25)
    if t == "REDUCED":
        return float(_cfg_get("REDUCED_TIER_WEIGHT", 1.0) or 1.0)
    if t == "MICRO":
        return float(_cfg_get("MICRO_TIER_WEIGHT", 0.6) or 0.6)
    return 0.0


def _build_entry_confidence(symbol: str, side: str, sig: dict, regime: str, research_universe: list) -> dict:
    w_ltf = int(_cfg_get("CONFIRM_WEIGHT_LTF", 30) or 30)
    w_htf = int(_cfg_get("CONFIRM_WEIGHT_HTF", 25) or 25)
    w_reg = int(_cfg_get("CONFIRM_WEIGHT_REGIME", 15) or 15)
    w_rank = int(_cfg_get("CONFIRM_WEIGHT_RANK", 15) or 15)
    w_sec = int(_cfg_get("CONFIRM_WEIGHT_SECTOR", 10) or 10)
    w_vol = int(_cfg_get("CONFIRM_WEIGHT_VOLUME", 5) or 5)

    comps = {}
    total = 0.0

    ltf_ok = bool(sig)
    comps["ltf"] = "ok" if ltf_ok else "fail"
    if ltf_ok:
        total += w_ltf

    htf_status = _htf_alignment_status(symbol, side, regime)
    comps["htf"] = htf_status.lower()
    if htf_status == "PASS":
        total += w_htf
    elif htf_status == "PARTIAL":
        total += (w_htf * 0.6)
    elif htf_status == "INSUFFICIENT_HISTORY":
        # Missing HTF history should be a moderate reduction, not a hard fail.
        if str(regime or "UNKNOWN").upper() == "SIDEWAYS":
            total += (w_htf * 0.5)
        else:
            total += (w_htf * 0.35)

    rg = str(regime or "UNKNOWN").upper()
    side_u = str(side or "BUY").upper()
    append_log("INFO", "HTF", f"[HTF] status={htf_status} symbol={symbol} side={side_u} regime={rg}")
    allowed, _reason, _meta = is_market_entry_allowed(symbol, rg, research_universe)
    comps["regime"] = "ok" if allowed else "weak"
    if rg in ("TRENDING", "TRENDING_UP"):
        if side_u == "BUY":
            total += w_reg if allowed else (w_reg * 0.7)
            comps["bias"] = "LONG_FIRST"
        else:
            total += (w_reg * 0.35) if allowed else (w_reg * 0.2)
            comps["bias"] = "SHORT_EXCEPTIONAL"
    elif rg in ("WEAK", "TRENDING_DOWN"):
        if side_u == "SHORT":
            total += w_reg if allowed else (w_reg * 0.7)
            comps["bias"] = "SHORT_FIRST"
        else:
            total += (w_reg * 0.35) if allowed else (w_reg * 0.2)
            comps["bias"] = "LONG_EXCEPTIONAL"
    elif rg == "SIDEWAYS":
        total += (w_reg * 0.75) if allowed else (w_reg * 0.45)
        comps["bias"] = "BALANCED"
    elif rg == "VOLATILE":
        total += (w_reg * 0.6) if allowed else (w_reg * 0.35)
        comps["bias"] = "RISK_REDUCED"
    else:
        total += (w_reg * 0.5) if allowed else (w_reg * 0.25)
        comps["bias"] = "UNKNOWN"

    rank = get_research_rank(symbol, research_universe)
    rscore = _research_score_for_symbol(symbol)
    rank_ok = (rank > 0 and rank <= 10) or (rscore is not None and float(rscore) >= 0.85)
    comps["rank"] = "strong" if rank_ok else "weak"
    if rank_ok:
        total += w_rank
    elif rank > 0 and rank <= 20:
        total += (w_rank * 0.5)

    sec_map = _sector_strength_snapshot(research_universe)
    sec = _sector_for_symbol(symbol)
    sec_strength = float(sec_map.get(sec, 0.0)) if isinstance(sec_map, dict) else 0.0
    comps["sector"] = "strong" if sec_strength >= 0 else "weak"
    if sec_strength >= 0.5:
        total += w_sec
    elif sec_strength >= 0:
        total += (w_sec * 0.6)
    elif sec_strength > -0.5:
        total += (w_sec * 0.3)

    q = _quality_metrics(symbol)
    vol_score = float(q.get("vol_score") or 0.0) if q.get("ok") else 0.0
    comps["volume"] = "ok" if vol_score >= 1.0 else "weak"
    if vol_score >= 1.0:
        total += w_vol
    elif vol_score >= 0.8:
        total += (w_vol * 0.5)

    total = total * _session_quality_score()
    if rg == "VOLATILE":
        total = total * 0.75
    comps["session"] = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()

    score = int(round(max(0.0, min(100.0, total))))
    tier = _entry_tier_from_score(score)

    hard_block_reason = ""
    om = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    om_reason = str((STATE.get("opening_metrics") or {}).get("reason") or "")
    if om == "OPEN_HARD_BLOCK" and om_reason in ("confirmed_broken_feed", "confirmed_extreme_gap"):
        hard_block_reason = om_reason

    if tier == "MICRO":
        micro_n = int(getattr(CFG, "ENTRY_MICRO_TOP_N", 5) or 5)
        if research_universe and not is_top_ranked_symbol(symbol, research_universe, micro_n):
            tier = "BLOCK"
            score = min(score, int(getattr(CFG, "ENTRY_MICRO_MIN_SCORE", 45) or 45) - 1)
            comps["micro_rank"] = "fail"

    if _is_micro_mode_active() and not hard_block_reason:
        max_trades = int(getattr(CFG, "MICRO_MODE_MAX_TRADES", 2) or 2)
        done = int(STATE.get("micro_mode_trade_count") or 0)
        if done < max_trades:
            min_score = int(getattr(CFG, "MICRO_MODE_MIN_SCORE", 35) or 35)
            micro_n = int(getattr(CFG, "ENTRY_MICRO_TOP_N", 10) or 10)
            top_ranked = (not research_universe) or is_top_ranked_symbol(symbol, research_universe, micro_n)
            # Score override: symbols ranked just outside top-N but with high score still qualify.
            score_override_n = int(getattr(CFG, "ENTRY_MICRO_SCORE_OVERRIDE_N", 20) or 20)
            score_override_min = int(getattr(CFG, "ENTRY_MICRO_SCORE_OVERRIDE_MIN", 65) or 65)
            if not top_ranked and score >= score_override_min:
                top_ranked = is_top_ranked_symbol(symbol, research_universe, score_override_n)
            if score >= min_score and top_ranked and tier == "BLOCK":
                tier = "MICRO"
                comps["micro_override"] = "active"

    size_mult = _entry_tier_multiplier(tier)
    if _is_micro_mode_active() and tier == "MICRO":
        size_mult = min(size_mult, float(getattr(CFG, "MICRO_MODE_SIZE_MULTIPLIER", 0.25) or 0.25))

    if tier == "BLOCK" and rg == "SIDEWAYS" and htf_status in ("PARTIAL", "INSUFFICIENT_HISTORY") and ltf_ok:
        # Score-tiered softening: high-confidence signals get REDUCED, lower get MICRO.
        # Previously all SIDEWAYS HTF-partial setups collapsed to MICRO (60% size).
        sideways_reduced_min = int(_cfg_get("SIDEWAYS_REDUCED_MIN_SCORE", 70) or 70)
        if score >= sideways_reduced_min:
            tier = "REDUCED"
            size_mult = _entry_tier_multiplier("REDUCED")
            comps["sideways_htf_softened"] = f"REDUCED(score={score})"
        else:
            tier = "MICRO"
            score = max(score, int(_cfg_get("ENTRY_MICRO_MIN_SCORE", 45) or 45))
            size_mult = _entry_tier_multiplier("MICRO")
            comps["sideways_htf_softened"] = htf_status.lower()

    return {
        "score": score,
        "tier": tier,
        "size_mult": size_mult,
        "components": comps,
        "hard_block": hard_block_reason,
    }


def _prune_micro_mode_events(now_ts: float | None = None):
    now_ts = float(now_ts or time.time())
    lookback_min = int(getattr(CFG, "MICRO_MODE_LOOKBACK_MINUTES", 45) or 45)
    cutoff = now_ts - (max(1, lookback_min) * 60)
    sig = [float(x) for x in list(STATE.get("signal_event_ts") or []) if float(x) >= cutoff]
    ent = [float(x) for x in list(STATE.get("entry_event_ts") or []) if float(x) >= cutoff]
    STATE["signal_event_ts"] = sig
    STATE["entry_event_ts"] = ent
    STATE["signals_seen_window"] = len(sig)
    STATE["entries_executed_window"] = len(ent)


def _record_signal_seen():
    now_ts = time.time()
    events = list(STATE.get("signal_event_ts") or [])
    events.append(now_ts)
    STATE["signal_event_ts"] = events
    _prune_micro_mode_events(now_ts)


def _record_entry_executed():
    now_ts = time.time()
    events = list(STATE.get("entry_event_ts") or [])
    events.append(now_ts)
    STATE["entry_event_ts"] = events
    _prune_micro_mode_events(now_ts)
    if bool(STATE.get("micro_mode_active")):
        STATE["micro_mode_trade_count"] = int(STATE.get("micro_mode_trade_count") or 0) + 1


def _is_micro_mode_active() -> bool:
    return bool(STATE.get("micro_mode_active"))


def _deactivate_micro_mode(reason: str = ""):
    if not bool(STATE.get("micro_mode_active")):
        return
    STATE["micro_mode_active"] = False
    STATE["micro_mode_trade_count"] = 0
    STATE["micro_mode_regime"] = ""
    append_log("INFO", "HEALTH", f"MICRO MODE deactivated{(' reason=' + reason) if reason else ''}")


def _overfilter_health_check():
    _prune_micro_mode_events()
    sig_n = int(STATE.get("signals_seen_window") or 0)
    ent_n = int(STATE.get("entries_executed_window") or 0)
    no_entry_cycles = int(STATE.get("no_entry_cycles") or 0)
    sig_thr = int(getattr(CFG, "OVERFILTER_SIGNAL_THRESHOLD", 8) or 8)
    cyc_thr = int(getattr(CFG, "OVERFILTER_NO_ENTRY_CYCLES", 6) or 6)
    micro_sig_thr = int(getattr(CFG, "MICRO_MODE_SIGNAL_THRESHOLD", 5) or 5)

    if sig_n >= sig_thr and ent_n == 0 and no_entry_cycles >= cyc_thr:
        if str(STATE.get("confirm_strictness") or "STRICT").upper() != "MODERATE":
            STATE["confirm_strictness"] = "MODERATE"
            append_log("WARN", "HEALTH", "Over-filtered mode detected → downgrading strict HTF to moderate")
            _notify("[HEALTH] Over-filtered: valid signals detected but no executions. Downgrading strict HTF to moderate.")

    if sig_n >= micro_sig_thr and ent_n == 0 and no_entry_cycles >= cyc_thr:
        if not bool(STATE.get("micro_mode_active")):
            STATE["micro_mode_active"] = True
            STATE["micro_mode_trade_count"] = 0
            STATE["micro_mode_regime"] = str(STATE.get("last_regime") or "UNKNOWN")
            append_log("WARN", "HEALTH", "over-filter detected -> activating MICRO MODE")
            _notify("[HEALTH] over-filter detected -> activating MICRO MODE")

    if ent_n > 0:
        STATE["confirm_strictness"] = "STRICT"
        STATE["signals_seen_window"] = 0
        STATE["entries_executed_window"] = 0
        if bool(STATE.get("micro_mode_active")):
            _deactivate_micro_mode("normal_trades_resumed")


def generate_short_signal(symbol: str, strategy_family: str = "short_breakdown"):
    sym = (symbol or "").strip().upper()
    q = _quality_metrics(sym)
    if not q.get("ok"):
        STATE.setdefault("last_short_reject_reasons", {})[sym] = "quality_metrics_unavailable"
        append_log("INFO", "SIG", f"family={strategy_family} symbol={sym} reject=quality_metrics_unavailable")
        return None
    price = float(q["price"])
    sma20 = float(q["sma20"])
    sma20_prev = float(q["sma20_prev"])
    vol_score = float(q["vol_score"])
    rs_vs_nifty = q.get("rs_vs_nifty")
    max_rs_short = float(getattr(CFG, "SHORT_RS_MAX_VS_NIFTY", -0.2) or -0.2)
    base_rs_ok = (rs_vs_nifty is None) or (float(rs_vs_nifty) <= max_rs_short)
    regime_u = str(STATE.get("last_regime") or "UNKNOWN").upper()
    trend_u = str(STATE.get("last_trend_direction") or "UNKNOWN").upper()
    entry_mode_u = str(get_regime_entry_mode(regime_u) or "UNKNOWN").upper()
    short_relaxed = regime_u == "WEAK" and trend_u == "DOWN" and entry_mode_u == "SHORT_PRIMARY"
    vol_relax_factor = float(getattr(CFG, "SHORT_WEAK_DOWN_VOL_RELAX_FACTOR", 0.90) or 0.90) if short_relaxed else 1.0
    sma_tol_pct = float(getattr(CFG, "SHORT_SMA20_TOLERANCE_PCT", 0.12) or 0.12)
    sma_tol = max(0.0, sma20 * (sma_tol_pct / 100.0))
    bearish_slope = sma20 < sma20_prev
    below_or_near_sma = (price < sma20) or (abs(price - sma20) <= sma_tol and bearish_slope)
    if short_relaxed:
        append_log("INFO", "SIG", f"short_relaxed_filters=1 regime={regime_u} trend={trend_u} entry_mode={entry_mode_u} vol_relax_factor={vol_relax_factor:.2f} sma_tol_pct={sma_tol_pct:.2f}")
    fam = str(strategy_family or "short_breakdown").strip().lower()
    reason = ""
    if fam == "outlier_short":
        outlier_rs = (rs_vs_nifty is None) or (float(rs_vs_nifty) <= (max_rs_short - 0.3))
        outlier_vol = vol_score > (max(1.4, float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2)) * vol_relax_factor)
        outlier_dist = price < (sma20 * 0.997)
        cond = below_or_near_sma and bearish_slope and outlier_vol and outlier_rs and outlier_dist
        if not cond:
            if not below_or_near_sma:
                reason = "price_not_below_sma20"
            elif not bearish_slope:
                reason = "sma20_not_bearish"
            elif not outlier_vol:
                reason = "volume_score_below_threshold"
            elif not outlier_rs:
                reason = "rs_vs_nifty_not_weak_enough"
            else:
                reason = "strategy_family_conditions_not_met"
    elif fam == "fallback_short":
        fallback_vol = vol_score > (float(getattr(CFG, "FALLBACK_MIN_VOLUME_SCORE", 1.2) or 1.2) * vol_relax_factor)
        cond = below_or_near_sma and fallback_vol and base_rs_ok and bearish_slope
        if not cond:
            if not below_or_near_sma:
                reason = "price_not_below_sma20"
            elif not bearish_slope:
                reason = "sma20_not_bearish"
            elif not fallback_vol:
                reason = "volume_score_below_threshold"
            elif not base_rs_ok:
                reason = "rs_vs_nifty_not_weak_enough"
            else:
                reason = "strategy_family_conditions_not_met"
    else:
        short_vol_thr = float(getattr(CFG, "SHORT_MIN_VOLUME_SCORE", 1.2) or 1.2) * vol_relax_factor
        cond = below_or_near_sma and bearish_slope and vol_score > short_vol_thr and base_rs_ok
        fam = "short_breakdown"
        if not cond:
            if not below_or_near_sma:
                reason = "price_not_below_sma20"
            elif not bearish_slope:
                reason = "sma20_not_bearish"
            elif not (vol_score > short_vol_thr):
                reason = "volume_score_below_threshold"
            elif not base_rs_ok:
                reason = "rs_vs_nifty_not_weak_enough"
            else:
                reason = "strategy_family_conditions_not_met"
    if not cond:
        rej = reason or "strategy_family_conditions_not_met"
        STATE.setdefault("last_short_reject_reasons", {})[sym] = rej
        append_log("INFO", "SIG", f"family={fam} symbol={sym} reject={rej}")
        return None
    STATE.setdefault("last_short_reject_reasons", {}).pop(sym, None)
    append_log("INFO", "SIG", f"family={fam} symbol={sym} setup=short_signal")
    return {
        "symbol": sym,
        "entry": price,
        "side": "SHORT",
        "volume_score": vol_score,
        "rs_vs_nifty": rs_vs_nifty,
        "strategy_family": fam,
    }


def _fallback_candidate_score(symbol: str) -> float | None:
    q = _quality_metrics(symbol)
    if not q.get("ok"):
        return None
    price = float(q.get("price") or 0.0)
    sma20 = float(q.get("sma20") or 0.0)
    sma20_prev = float(q.get("sma20_prev") or 0.0)
    if price <= 0 or sma20 <= 0:
        return None
    trend_component = 1.0 if (price > sma20 and sma20 >= sma20_prev) else 0.0
    dist_component = max(-3.0, min(3.0, ((price - sma20) / sma20) * 100.0)) / 3.0
    vol_component = max(0.0, min(2.0, float(q.get("vol_score") or 0.0))) / 2.0
    rs = q.get("rs_vs_nifty")
    rs_component = 0.0 if rs is None else max(-3.0, min(3.0, float(rs))) / 3.0
    return (0.40 * trend_component) + (0.25 * dist_component) + (0.20 * vol_component) + (0.15 * rs_component)


def build_fallback_universe() -> list:
    base = load_universe_live() or load_universe_trading() or []
    top_n = int(getattr(CFG, "FALLBACK_TOP_N", 10) or 10)
    scored = []
    for sym in list(dict.fromkeys([s for s in base if s])):
        score = _fallback_candidate_score(sym)
        if score is not None:
            scored.append((sym, float(score)))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in scored[: max(10, top_n)]]
    if not out:
        out = list(dict.fromkeys([s for s in base if s]))[: max(10, top_n)]
    STATE["fallback_universe"] = out
    append_log("INFO", "UNIV", f"fallback universe built size={len(out)} scored={len(scored)}")
    return out


def _maybe_enter_short_from_signal(sig):
    if not sig:
        return False
    # CNC (delivery) does not support short selling on Zerodha.
    # Only MIS (intraday) allows shorting.
    if str(STATE.get("trading_mode") or "INTRADAY").upper() == "SWING":
        append_log("INFO", "SKIP", "Short blocked: SWING/CNC mode does not support short selling")
        return False
    _record_signal_seen()
    sym = str(sig.get("symbol") or "").strip().upper()
    entry = float(sig.get("entry") or 0.0)
    strategy_family = str(sig.get("strategy_family") or "short_breakdown")
    universe_source = str(sig.get("universe_source") or "primary")
    if not sym or entry <= 0:
        return False

    # Dynamic short limit: up to half of max concurrent slots (min 2).
    # Scales with wallet — 10k wallet with 3 slots → max 2 shorts;
    # 50k wallet with 6 slots → max 3 shorts.
    max_short = max(2, _dynamic_max_concurrent() // 2)
    if _open_short_positions_count() >= max_short:
        append_log("WARN", "RISK", "max short positions reached")
        return False

    research_universe = _active_trade_universe()
    regime = str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN")
    decision = _build_entry_confidence(sym, "SHORT", sig, regime, research_universe)
    append_log(
        "INFO",
        "CONFIRM",
        f"symbol={sym} family={strategy_family} score={decision['score']} tier={decision['tier']} "
        f"htf={decision['components'].get('htf')} regime={decision['components'].get('regime')} "
        f"rank={decision['components'].get('rank')}",
    )
    if decision.get("hard_block"):
        append_log("WARN", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason={decision['hard_block']}")
        return False
    if decision.get("tier") == "BLOCK":
        htf_comp = str(decision.get("components", {}).get("htf") or "").lower()
        if htf_comp == "fail":
            append_log("INFO", "CONFIRM", f"SHORT blocked {sym} reason=HTF_Score_Below_Req_for_{regime} htf_status=FAIL htf_score=0")
        append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
        return False
    tier_mult = float(decision.get("size_mult") or 0.0)
    strategy_tag = "short_breakdown"
    if bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)):
        strategy_tag = "mtf_confirmed_short"
    qty, bucket_qty, risk_qty = _calc_qty(
        sym,
        entry,
        tier=str(decision.get("tier") or "BLOCK"),
        tier_weight=tier_mult,
        side="SELL",
        regime=regime,
        trend_direction=str((get_market_regime_snapshot() or {}).get("trend_direction") or STATE.get("last_trend_direction") or "UNKNOWN"),
        family=str(strategy_family),
    )
    qty = _apply_strategy_allocation(
        qty,
        strategy_tag,
        tier=str(decision.get("tier") or ""),
        side="SHORT",
        regime=regime,
        trend_direction=str((get_market_regime_snapshot() or {}).get("trend_direction") or STATE.get("last_trend_direction") or "UNKNOWN"),
    )
    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero_post_allocation strategy={strategy_tag}")
        return False
    append_log("INFO", "CONFIRM", f"symbol={sym} tier={decision['tier']} size_weight_applied={tier_mult:.2f}")

    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if _in_open_filter_window() and mode in ("OPEN_HARD_BLOCK", "OPEN_UNSAFE", "OPEN_MODERATE", "OPEN_CLEAN"):
        allowed_open, open_reason = _opening_selective_entry_allowed(sym, side="SHORT")
        if not allowed_open:
            append_log("WARN", "OPEN", f"blocked {sym} reason={open_reason}")
            return False
        open_mult = _opening_size_multiplier()
        if open_reason == "fallback_min_trade":
            append_log("INFO", "OPEN", "fallback min-trade activated after prolonged no-exec cycles")
            qty = 1
        else:
            qty = max(1, int(math.floor(qty * open_mult))) if qty > 0 and open_mult > 0 else 0
        if mode == "OPEN_UNSAFE":
            append_log("INFO", "OPEN", f"mode=OPEN_UNSAFE → micro-size selective entry allowed mult={open_mult:.2f}")
        elif mode == "OPEN_MODERATE":
            append_log("INFO", "OPEN", f"mode=OPEN_MODERATE → reduced-size entry allowed mult={open_mult:.2f}")
        else:
            append_log("INFO", "OPEN", "mode=OPEN_CLEAN → normal early-session trading allowed")

    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
        try:
            SA.record_skipped_signal({"symbol": sym, "side": "SHORT", "reason": "qty_zero", "strategy_tag": strategy_tag, "signal_price": entry})
        except Exception:
            pass
        _apply_skip_cooldown(sym, "qty_zero")
        return False
    min_notional = float(getattr(CFG, "MIN_MEANINGFUL_NOTIONAL_INR", 0.0) or 0.0)
    if min_notional > 0 and (entry * qty) < min_notional:
        append_log("INFO", "SKIP", f"{sym} reason=below_min_meaningful_notional notional={entry*qty:.2f} min={min_notional:.2f}")
        return False

    if not _can_open_new_trade(sym, entry, qty, momentum_positive=False):
        return False

    if sym in _positions():
        append_log("WARN", "SKIP", f"{sym} reason=already_held_pre_order side=SHORT")
        return False

    mode = "LIVE" if is_live_enabled() else "PAPER"
    oid = None
    booked_entry = entry
    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            booked_entry = now_price
        # Shorts are ALWAYS intraday — never routed to swing/CNC regardless of mode.
        _short_regime = str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN")
        append_log(
            "INFO", "ROUTE",
            f"mode_route=INTRADAY symbol={sym} side=SHORT product=MIS "
            f"strategy={strategy_tag} family={strategy_family} tier={decision.get('tier')} "
            f"regime={_short_regime} risk_profile={current_risk_profile()} reason=short_always_intraday"
        )
        append_log("INFO", "ORDER", f"symbol={sym} side=SELL family={strategy_family} tier={decision.get('tier')} qty={qty} entry={booked_entry:.2f} product=MIS trade_mode=INTRADAY risk_profile={current_risk_profile()}")
        oid = _place_live_order(kite, sym, "SELL", qty, product_override="MIS")
        if not oid:
            append_log("WARN", "SKIP", f"{sym} reason=order_failed side=SHORT qty={qty}")
            return False
        fill_price = _wait_for_fill(kite, oid, booked_entry)
        if fill_price is None:
            append_log("ERROR", "SKIP", f"{sym} reason=order_rejected_or_cancelled side=SHORT order_id={oid}")
            return False
        booked_entry = fill_price
        append_log("INFO", "FILL", f"symbol={sym} side=SELL qty={qty} fill={booked_entry:.2f} order_id={oid} product=MIS trade_mode=INTRADAY")
    else:
        append_log(
            "INFO", "ROUTE",
            f"mode_route=INTRADAY symbol={sym} side=SHORT product=MIS "
            f"strategy={strategy_tag} family={strategy_family} tier={decision.get('tier')} "
            f"risk_profile={current_risk_profile()} reason=short_always_intraday [paper]"
        )

    PM.set(sym, {
        "symbol": sym,
        "side": "SHORT",
        "entry": booked_entry,
        "entry_price": booked_entry,
        "qty": qty,
        "quantity": qty,
        "peak": 0.0,
        "peak_pct": 0.0,
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
        "order_id": oid,
        "product": "MIS",
        "trade_mode": "INTRADAY",
        "risk_profile_at_entry": current_risk_profile(),
        "strategy_tag": strategy_tag,
        "strategy_family": strategy_family,
        "confidence_tier": str(decision.get("tier") or "BLOCK"),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
        "market_regime": str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN"),
        "universe_source": universe_source,
        "sector": _sector_for_symbol(sym),
        "entry_time": datetime.now(IST).isoformat(timespec="seconds"),
        "entry_atr": float(sig.get("atr") or 0.0),
    })
    STATE["entry_tier_for_cooldown"] = str(decision.get("tier") or "").upper()
    _set_cooldown()
    _log_trade_event("ORDER", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("FILL", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("TRADE", {**dict(_positions().get(sym) or {}), "symbol": sym, "exit_reason": "-"})
    _append_runtime_event("recent_entries", {"symbol": sym, "side": "SHORT", "qty": qty, "entry": booked_entry, "family": strategy_family, "tier": str(decision.get("tier") or "n/a"), "ts": datetime.now(IST).isoformat(timespec="seconds")}, limit=40)
    append_log("INFO", "ENTRY", f"SHORT {sym} family={strategy_family} tier={decision.get('tier')} source={universe_source} qty={qty} entry={booked_entry:.2f}")
    append_log("INFO", "EXEC", f"symbol={sym} side=SHORT size={int(round(tier_mult * 100))}%")
    if _is_micro_mode_active() and decision.get("tier") == "MICRO":
        append_log("INFO", "MICRO", f"executing symbol={sym} score={decision['score']} size={int(round(tier_mult * 100))}%")
    _notify(f"🟠 SHORT {mode}\nSymbol: {sym}\nQuantity: {qty}\nEntry: {booked_entry:.2f}")
    _record_entry_executed()
    return True

def _maybe_enter_from_signal(sig):
    if not sig:
        return False
    _record_signal_seen()
    sym = sig["symbol"].strip().upper()
    strategy_family = str(sig.get("strategy_family") or "trend_long")
    universe_source = str(sig.get("universe_source") or "primary")
    append_log("INFO", "SCAN", f"Scanning {sym}")

    if _skip_cooldown_active(sym):
        append_log("INFO", "SKIP", f"{sym} reason=skip_cooldown")
        return False

    # 1) Market regime check (requested before buy gating)
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    trend_direction = str(snap.get("trend_direction", STATE.get("last_trend_direction", "UNKNOWN")) or "UNKNOWN").upper()
    research_universe = _active_trade_universe()
    allowed, reason, meta = is_market_entry_allowed(sym, regime, research_universe)
    weak_score = float(sig.get("signal_score") or 0.0)
    weak_score_min = float(getattr(CFG, "WEAK_MARKET_MIN_SCORE", 0.75) or 0.75)
    weak_size_mult = float(getattr(CFG, "WEAK_MARKET_SIZE_MULTIPLIER", 0.5) or 0.5)
    weak_long_allowed = True

    if regime == "WEAK":
        if weak_score >= weak_score_min:
            append_log("INFO", "MARKET", f"[MARKET] WEAK regime long allowed sym={sym} score={weak_score:.2f} size_mult={weak_size_mult:.1f}")
        else:
            append_log("INFO", "SKIP", f"[SKIP] {sym} reason=weak_regime_score_too_low score={weak_score:.2f} threshold={weak_score_min:.2f}")
            weak_long_allowed = False

    if regime in ("WEAK", "TRENDING_DOWN"):
        if not allowed:
            append_log("INFO", "MARKET", f"regime={regime} → long kept exceptional for {sym} reason={reason}")
            weak_cd = int(getattr(CFG, "MARKET_WEAK_COOLDOWN_MIN", 3) or 3)
            weak_cd = max(2, min(5, weak_cd))
            _apply_skip_cooldown(sym, "market_weak", minutes=weak_cd)
        else:
            append_log("INFO", "MARKET", f"regime={regime} → selective entry allowed for {sym} rank={meta.get('rank')} score={float(meta.get('score') or 0.0):.2f}")
    if regime == "WEAK" and not weak_long_allowed:
        return False
    elif regime == "UNKNOWN" and bool(getattr(CFG, "BLOCK_ON_UNKNOWN_MARKET_REGIME", False)):
        append_log("WARN", "MARKET", f"regime=UNKNOWN → blocked {sym} reason=unknown_regime")
        return False

    if strategy_family in ("mean_reversion", "fallback_long") and regime == "SIDEWAYS" and trend_direction == "DOWN":
        mode = str(getattr(CFG, "SIDEWAYS_DOWN_MR_MODE", "REDUCED") or "REDUCED").upper()
        hh, mm = _parse_hhmm(str(getattr(CFG, "MEAN_REVERSION_CUTOFF_HHMM", "11:30") or "11:30"))
        now_t = datetime.now(IST).time()
        cutoff_t = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0).time()
        exceptional = float(sig.get("signal_score") or 0.0) >= float(getattr(CFG, "MEAN_REVERSION_EXCEPTIONAL_SCORE", 0.90) or 0.90)
        q = _quality_metrics(sym)
        reclaim_ok = bool(q.get("ok")) and float(q.get("price") or 0.0) > float(q.get("sma20") or 0.0) and float(q.get("vol_score") or 0.0) >= float(getattr(CFG, "MEAN_REVERSION_MIN_VOL_SCORE", 1.0) or 1.0)
        if now_t >= cutoff_t and not exceptional:
            append_log("INFO", "MARKET", f"blocked {sym} family={strategy_family} reason=mr_cutoff_sideways_down trend={trend_direction}")
            return False
        if not reclaim_ok:
            append_log("INFO", "MARKET", f"blocked {sym} family={strategy_family} reason=mr_reclaim_missing trend={trend_direction}")
            return False
        if mode == "BLOCK":
            append_log("INFO", "MARKET", f"blocked {sym} family={strategy_family} reason=mr_sideways_down_block trend={trend_direction}")
            return False

    # 2) Universe membership check against active dynamic universe (if available)
    active_dyn = _active_trade_universe()
    if active_dyn and sym not in set(active_dyn):
        append_log("INFO", "UNIV", f"{sym} not in research universe -> skip")
        return False

    # 3) Sector filter / cap
    if not _passes_sector_entry_filter(sym):
        return False

    entry = float(sig.get("entry") or 0.0)
    if entry <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=invalid_entry")
        return False

    momentum_pct = float(sig.get("momentum_pct") or _compute_symbol_momentum_pct(sym) or 0.0)
    momentum_threshold = float(getattr(CFG, "REENTRY_MOMENTUM_MIN_PCT", 0.0))
    momentum_positive = momentum_pct > momentum_threshold

    strategy_tag = "primary_long"
    decision = _build_entry_confidence(sym, "BUY", sig, regime, research_universe)
    append_log(
        "INFO",
        "CONFIRM",
        f"symbol={sym} family={strategy_family} score={decision['score']} tier={decision['tier']} "
        f"htf={decision['components'].get('htf')} regime={decision['components'].get('regime')} "
        f"rank={decision['components'].get('rank')}",
    )
    if decision.get("hard_block"):
        append_log("WARN", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason={decision['hard_block']}")
        return False
    if decision.get("tier") == "BLOCK":
        htf_comp = str(decision.get("components", {}).get("htf") or "").lower()
        if htf_comp == "fail":
            dscore = float(decision.get("score") or 0.0)
            if regime == "WEAK" and 30.0 <= dscore <= 60.0:
                tier_mult = max(0.0, weak_size_mult)
                append_log("INFO", "CONFIRM", f"BUY soft-allowed {sym} reason=HTF_Score_Below_Req_for_WEAK score={dscore:.0f} size_mult={tier_mult:.2f}")
            else:
                append_log("INFO", "CONFIRM", f"BUY blocked {sym} reason=HTF_Score_Below_Req_for_{regime} htf_status=FAIL htf_score=0")
                append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
                return False
        elif regime == "SIDEWAYS" and htf_comp in ("partial", "insufficient_history"):
            dscore = float(decision.get("score") or 0.0)
            sideways_reduced_min = int(_cfg_get("SIDEWAYS_REDUCED_MIN_SCORE", 70) or 70)
            if dscore >= sideways_reduced_min:
                tier_mult = max(0.0, _entry_tier_multiplier("REDUCED"))
                decision["tier"] = "REDUCED"
                append_log("INFO", "CONFIRM", f"BUY soft-allowed {sym} reason=HTF_{htf_comp.upper()}_SIDEWAYS tier=REDUCED score={dscore:.0f}")
            else:
                tier_mult = max(0.0, _entry_tier_multiplier("MICRO"))
                decision["tier"] = "MICRO"
                append_log("INFO", "CONFIRM", f"BUY soft-allowed {sym} reason=HTF_{htf_comp.upper()}_SIDEWAYS tier=MICRO score={dscore:.0f}")
        else:
            append_log("INFO", "CONFIRM", f"symbol={sym} score={decision['score']} tier=BLOCK reason=low_total_confidence")
            return False
    else:
        tier_mult = float(decision.get("size_mult") or 0.0)
    if strategy_family in ("mean_reversion", "fallback_long") and regime == "SIDEWAYS" and trend_direction == "DOWN":
        # Default changed from BLOCK to REDUCED — SIDEWAYS+DOWN has mean-reversion edge.
        # BLOCK was leaving profitable setups entirely untouched.
        mode_sd = str(getattr(CFG, "SIDEWAYS_DOWN_MR_MODE", "REDUCED") or "REDUCED").upper()
        if mode_sd in ("MICRO", "REDUCED"):
            forced_tier = mode_sd if mode_sd in ("MICRO", "REDUCED") else "MICRO"
            decision["tier"] = forced_tier
            tier_mult = min(max(0.0, tier_mult), _entry_tier_multiplier(forced_tier))
            append_log("INFO", "MARKET", f"family={strategy_family} regime=SIDEWAYS trend=DOWN forced_tier={forced_tier}")
    qty, bucket_qty, risk_qty = _calc_qty(
        sym,
        entry,
        tier=str(decision.get("tier") or "BLOCK"),
        tier_weight=tier_mult,
        side="BUY",
        regime=regime,
        trend_direction=trend_direction,
        family=str(strategy_family),
    )
    if regime in ("WEAK", "TRENDING_DOWN"):
        mult = weak_size_mult
        if mult > 0:
            reduced = max(1, int(math.floor(qty * mult)))
            if reduced < qty:
                qty = reduced
                append_log("INFO", "MARKET", f"regime={regime} → size reduced multiplier={mult}")
        strategy_tag = "weak_market_long"
    if bool(getattr(CFG, "USE_MTF_CONFIRMATION", True)) and strategy_tag in ("primary_long", "weak_market_long"):
        strategy_tag = "mtf_confirmed_long"
    qty = _apply_strategy_allocation(
        qty,
        strategy_tag,
        tier=str(decision.get("tier") or ""),
        side="LONG",
        regime=regime,
        trend_direction=trend_direction,
    )
    # Guard: strategy allocation can reduce qty to 0 in edge cases not covered
    # by the floor override. Never submit a zero-qty order to the exchange.
    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero_post_allocation strategy={strategy_tag}")
        return False
    append_log("INFO", "CONFIRM", f"symbol={sym} tier={decision['tier']} size_weight_applied={tier_mult:.2f}")

    mode = str(STATE.get("opening_mode") or "OPEN_CLEAN").upper()
    if _in_open_filter_window() and mode in ("OPEN_HARD_BLOCK", "OPEN_UNSAFE", "OPEN_MODERATE", "OPEN_CLEAN"):
        allowed_open, open_reason = _opening_selective_entry_allowed(sym, side="BUY")
        if not allowed_open:
            append_log("WARN", "OPEN", f"blocked {sym} reason={open_reason}")
            return False
        open_mult = _opening_size_multiplier()
        if open_reason == "fallback_min_trade":
            append_log("INFO", "OPEN", "fallback min-trade activated after prolonged no-exec cycles")
            qty = 1
        else:
            qty = max(1, int(math.floor(qty * open_mult))) if qty > 0 and open_mult > 0 else 0
        if mode == "OPEN_UNSAFE":
            append_log("INFO", "OPEN", f"mode=OPEN_UNSAFE → micro-size selective entry allowed mult={open_mult:.2f}")
        elif mode == "OPEN_MODERATE":
            append_log("INFO", "OPEN", f"mode=OPEN_MODERATE → reduced-size entry allowed mult={open_mult:.2f}")
        else:
            append_log("INFO", "OPEN", "mode=OPEN_CLEAN → normal early-session trading allowed")

    if qty <= 0:
        append_log("INFO", "SKIP", f"{sym} reason=qty_zero bucket_qty={bucket_qty} risk_qty={risk_qty}")
        try:
            SA.record_skipped_signal({"symbol": sym, "side": "BUY", "reason": "qty_zero", "strategy_tag": strategy_tag, "signal_price": entry, "market_regime": regime})
        except Exception:
            pass
        _apply_skip_cooldown(sym, "qty_zero")
        return False
    min_notional = float(getattr(CFG, "MIN_MEANINGFUL_NOTIONAL_INR", 0.0) or 0.0)
    if min_notional > 0 and (entry * qty) < min_notional:
        append_log("INFO", "SKIP", f"{sym} reason=below_min_meaningful_notional notional={entry*qty:.2f} min={min_notional:.2f}")
        return False

    if not _can_open_new_trade(sym, entry, qty, momentum_positive=momentum_positive):
        return False

    # Final guard: re-check position doesn't exist immediately before placing order.
    # Prevents double-entry if two signals arrive in the same tick.
    if sym in _positions():
        append_log("WARN", "SKIP", f"{sym} reason=already_held_pre_order")
        return False

    mode = "LIVE" if is_live_enabled() else "PAPER"
    oid = None
    booked_entry = entry
    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            if now_price > entry * (1.0 + max_slip):
                append_log("INFO", "SKIP", f"{sym} reason=slippage")
                return False
            booked_entry = now_price
        # Per-trade mode routing (INTRADAY/SWING/HYBRID-aware).
        _signal_ctx = {
            "side": "BUY",
            "strategy_tag": strategy_tag,
            "strategy_family": strategy_family,
            "tier": str(decision.get("tier") or ""),
            "regime": regime,
            "trend_direction": str(STATE.get("last_trend_direction") or ""),
            "weak_market_exception": bool(STATE.get("weak_market_entry_active")),
        }
        _trade_mode, _route_reason = classify_trade_mode(_signal_ctx)
        _trade_product = product_for_trade_mode(_trade_mode)
        append_log(
            "INFO", "ROUTE",
            f"mode_route={_trade_mode} symbol={sym} side=BUY product={_trade_product} "
            f"strategy={strategy_tag} family={strategy_family} tier={decision.get('tier')} "
            f"regime={regime} risk_profile={current_risk_profile()} reason={_route_reason}"
        )
        append_log("INFO", "ORDER", f"symbol={sym} side=BUY family={strategy_family} tier={decision.get('tier')} qty={qty} entry={booked_entry:.2f} product={_trade_product} trade_mode={_trade_mode} risk_profile={current_risk_profile()}")
        oid = _place_live_order(kite, sym, "BUY", qty, product_override=_trade_product)
        if not oid:
            append_log("WARN", "SKIP", f"{sym} reason=order_failed side=BUY qty={qty}")
            return False
        fill_price = _wait_for_fill(kite, oid, booked_entry)
        if fill_price is None:
            append_log("ERROR", "SKIP", f"{sym} reason=order_rejected_or_cancelled side=BUY order_id={oid}")
            return False
        booked_entry = fill_price
        append_log("INFO", "FILL", f"symbol={sym} side=BUY qty={qty} fill={booked_entry:.2f} order_id={oid} product={_trade_product} trade_mode={_trade_mode}")
    else:
        # PAPER mode still classifies so tests + status reflect routing decisions.
        _signal_ctx = {
            "side": "BUY",
            "strategy_tag": strategy_tag,
            "strategy_family": strategy_family,
            "tier": str(decision.get("tier") or ""),
            "regime": regime,
            "trend_direction": str(STATE.get("last_trend_direction") or ""),
            "weak_market_exception": bool(STATE.get("weak_market_entry_active")),
        }
        _trade_mode, _route_reason = classify_trade_mode(_signal_ctx)
        _trade_product = product_for_trade_mode(_trade_mode)
        append_log(
            "INFO", "ROUTE",
            f"mode_route={_trade_mode} symbol={sym} side=BUY product={_trade_product} "
            f"strategy={strategy_tag} family={strategy_family} tier={decision.get('tier')} "
            f"regime={regime} risk_profile={current_risk_profile()} reason={_route_reason} [paper]"
        )

    PM.set(sym, {
        "symbol": sym,
        "side": "BUY",
        "entry": booked_entry,
        "entry_price": booked_entry,
        "qty": qty,
        "quantity": qty,
        "peak": 0.0,
        "peak_pct": 0.0,
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
        "order_id": oid,
        "product": _trade_product,
        "trade_mode": _trade_mode,
        "risk_profile_at_entry": current_risk_profile(),
        "strategy_tag": strategy_tag,
        "strategy_family": strategy_family,
        "confidence_tier": str(decision.get("tier") or "BLOCK"),
        "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
        "market_regime": regime,
        "universe_source": universe_source,
        "sector": _sector_for_symbol(sym),
        "entry_time": datetime.now(IST).isoformat(timespec="seconds"),
        "entry_atr": float(sig.get("atr") or 0.0),
    })
    STATE["entry_tier_for_cooldown"] = str(decision.get("tier") or "").upper()
    _set_cooldown()
    _log_trade_event("ORDER", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("FILL", {**dict(_positions().get(sym) or {}), "symbol": sym})
    _log_trade_event("TRADE", {**dict(_positions().get(sym) or {}), "symbol": sym, "exit_reason": "-"})
    _append_runtime_event("recent_entries", {"symbol": sym, "side": "BUY", "qty": qty, "entry": booked_entry, "family": strategy_family, "tier": str(decision.get("tier") or "n/a"), "ts": datetime.now(IST).isoformat(timespec="seconds")}, limit=40)
    append_log("INFO", "SIG", f"BUY trigger {sym}")
    append_log("INFO", "ENTRY", f"BUY {sym} family={strategy_family} tier={decision.get('tier')} source={universe_source} qty={qty} mode={mode}")
    append_log("INFO", "EXEC", f"symbol={sym} side=BUY size={int(round(tier_mult * 100))}%")
    if _is_micro_mode_active() and decision.get("tier") == "MICRO":
        append_log("INFO", "MICRO", f"executing symbol={sym} score={decision['score']} size={int(round(tier_mult * 100))}%")
    _notify(
        f"🟢 BUY {mode}\n"
        f"Symbol: {sym}\n"
        f"Quantity: {qty}\n"
        f"Entry: {booked_entry:.2f}\n"
        f"Wallet: {float(STATE.get('wallet_net_inr') or 0.0):.2f}"
    )
    _record_entry_executed()
    return True


def get_positions_text():
    pos = _positions()
    if not pos:
        return "📍 Positions\n\nNo open positions."

    rows = []
    for sym, tr in sorted(pos.items()):
        entry, qty = _trade_entry_qty(tr)
        peak_pct = float(tr.get("peak_pct") or tr.get("peak") or 0.0)
        rows.append(f"- {sym} qty={qty} entry={entry:.2f} peak%={peak_pct:.2f}")

    return "📍 Positions\n\n" + "\n".join(rows)


def _current_open_pnl_breakdown():
    """Returns tuple: (profit_inr, loss_inr_abs) for currently open positions."""
    profit_inr = 0.0
    loss_inr_abs = 0.0
    kite = None
    try:
        kite = get_kite()
    except Exception:
        kite = None

    for sym, tr in sorted(_positions().items()):
        entry, qty = _trade_entry_qty(tr)
        if entry <= 0:
            continue
        ltp = _ltp(kite, sym) if kite else entry
        if ltp is None:
            ltp = entry
        side = str((tr or {}).get("side") or "LONG").upper()
        pnl, _ = _calc_pnl(entry, ltp, qty, side=side)
        if pnl >= 0:
            profit_inr += pnl
        else:
            loss_inr_abs += abs(pnl)

    return float(profit_inr), float(loss_inr_abs)


def _refresh_runtime_pnl_fields():
    _ensure_day_key()
    realized = float(STATE.get("today_pnl") or 0.0)
    prof, loss_abs = _current_open_pnl_breakdown()
    unrealized = float(prof - loss_abs)
    total = realized + unrealized
    STATE["realized_today"] = realized
    STATE["unrealized_now"] = unrealized
    STATE["pnl_so_far"] = total
    append_log("INFO", "PNL", f"realized_today={realized:.2f} unrealized_now={unrealized:.2f} pnl_so_far={total:.2f}")
    return realized, unrealized, total


def get_status_text():
    _ensure_day_key()
    _sync_wallet_and_caps(force=False)
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    rows = []
    for sym, p in sorted(_positions().items()):
        e, q = _trade_entry_qty(p)
        rows.append(
            f"- {sym} {str(p.get('side') or 'BUY').upper()} qty={q} entry={e:.2f} "
            f"trade_mode={str(p.get('trade_mode') or ('SWING' if str(p.get('product') or '').upper()=='CNC' else 'INTRADAY'))} "
            f"product={str(p.get('product') or '-').upper()} "
            f"strategy={p.get('strategy_tag','-')} family={p.get('strategy_family','-')} "
            f"tier={p.get('confidence_tier','-')} source={p.get('universe_source','-')} regime={p.get('market_regime','-')}"
        )
    realized, unrealized, pnl_so_far = _refresh_runtime_pnl_fields()
    active_top3 = ",".join(list(STATE.get("active_strategy_families") or [])) or "none"
    selector_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    runtime_ver = str(_cfg_get("RUNTIME_VERSION", "unknown") or "unknown")
    cu = dict(STATE.get("capital_utilization") or {})
    return (
        "📟 Trident Status\n\n"
        f"Runtime: {runtime_ver}\n"
        f"Mode: {mode}\n"
        f"Trading Mode: {current_trading_mode()} ({'CNC longs / MIS shorts' if current_trading_mode() == 'SWING' else ('per-trade MIS+CNC' if current_trading_mode() == 'HYBRID' else 'MIS ₹20/order')})\n"
        f"Risk Profile: {current_risk_profile()}"
        f"{' (pending GOD confirmation)' if str(STATE.get('pending_risk_profile_confirmation') or '').upper() == 'GOD' else ''}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(load_universe_trading())} symbols\n"
        f"Universe(live): {len(load_universe_live())} symbols\n"
        f"Open Positions: {_open_positions_count()}\n"
        f"Last Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Active Top3 Families: {active_top3}\n"
        f"Top3 Refresh Reason: {selector_reason}\n"
        f"Opening Mode: {STATE.get('opening_mode','OPEN_CLEAN')}\n"
        f"Realized Today: ₹{realized:.2f}\n"
        f"Unrealized Now: ₹{unrealized:.2f}\n"
        f"P/L So Far: ₹{pnl_so_far:.2f}\n\n"
        "Wallet/Caps:\n"
        f"- Wallet Net: ₹{float(STATE.get('wallet_net_inr') or 0):.2f}\n"
        f"- Wallet Available: ₹{float(STATE.get('wallet_available_inr') or 0):.2f}\n"
        f"- Exposure: ₹{_current_exposure_inr():.2f} / ₹{_max_exposure_inr():.2f} ({_effective_max_exposure_pct():.1f}%)\n"
        f"- Deployable: ₹{float(cu.get('deployable_capital') or 0.0):.2f}\n"
        f"- Unused Deployable: ₹{float(cu.get('unused_deployable') or 0.0):.2f}\n"
        f"- Utilization: {float(cu.get('utilization_pct') or 0.0):.1f}% | Avg Position Size: ₹{float(cu.get('avg_position_size') or 0.0):.2f}\n"
        f"- Daily Loss Cap (hard): ₹{float(STATE.get('daily_loss_cap_inr') or 0):.2f}\n"
        f"- Profit Milestone (soft): ₹{float(STATE.get('daily_profit_milestone_inr') or 0):.2f}\n\n"
        "Open Trades:\n"
        + ("\n".join(rows) if rows else "(none)")
        + "\n"
    )


def get_pnl_so_far_text() -> str:
    realized, unrealized, total = _refresh_runtime_pnl_fields()
    wallet = float(STATE.get("wallet_net_inr") or STATE.get("last_wallet") or getattr(CFG, "CAPITAL_INR", 0.0) or 0.0)
    pct = (total / wallet * 100.0) if wallet > 0 else 0.0
    icon = "🟢" if total >= 0 else "🔴"
    return (
        "💰 P/L So Far\n\n"
        f"Realized Today: ₹{realized:.2f}\n"
        f"Unrealized Now: ₹{unrealized:.2f}\n"
        f"{icon} Total (So Far): ₹{total:.2f} ({pct:+.2f}%)"
    )


def get_research_text(limit: int = 16) -> str:
    events = list(STATE.get("research_events") or [])[-max(1, int(limit)) :]
    rep = dict(STATE.get("research_last_report") or {})
    lines = ["🔬 Research", ""]
    if rep:
        lines.append(
            f"Night Research: generated={rep.get('generated_at','-')} selected={int(rep.get('selected_count') or 0)}"
        )
        lines.append(f"Top Symbols: {','.join(list(rep.get('top_symbols') or [])[:8]) or 'n/a'}")
        lines.append("")
    if not events:
        lines.append("No runtime research events yet.")
        return "\n".join(lines)
    for e in events:
        ts = str(e.get("ts") or "-")
        et = str(e.get("event") or "event")
        msg = str(e.get("message") or "")
        lines.append(f"- [{ts}] {et}: {msg}")
    return "\n".join(lines)


def get_universe_changes_text(limit: int = 14) -> str:
    rows = list(STATE.get("universe_changes_today") or [])[-max(1, int(limit)) :]
    if not rows:
        return "🌌 Universe Changes\n\nNo universe changes tracked yet today."
    lines = ["🌌 Universe Changes", ""]
    for r in rows:
        ts = str(r.get("ts") or "-")
        reason = str(r.get("reason") or "n/a")
        src = str(r.get("source") or "n/a")
        add = ",".join(list(r.get("added") or [])) or "-"
        rem = ",".join(list(r.get("removed") or [])) or "-"
        fb = bool(r.get("fallback_active"))
        lines.append(f"- [{ts}] reason={reason} src={src} add={add} remove={rem} fallback={fb}")
    return "\n".join(lines)


def get_analytics_text() -> str:
    realized, unrealized, total = _refresh_runtime_pnl_fields()
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    runtime_ver = str(_cfg_get("RUNTIME_VERSION", "unknown") or "unknown")
    top3 = ",".join(list(STATE.get("active_strategy_families") or [])) or "none"
    active_uni = ",".join(list(STATE.get("active_universe") or [])[:12]) or "none"
    open_rows = []
    for sym, p in sorted(_positions().items()):
        e, q = _trade_entry_qty(p)
        open_rows.append(f"{sym}:{str(p.get('side') or 'BUY').upper()} qty={q} entry={e:.2f}")
    entries = list(STATE.get("recent_entries") or [])[-5:]
    exits = list(STATE.get("recent_exits") or [])[-5:]
    ent_txt = "; ".join([f"{e.get('symbol')} {e.get('side')} q={e.get('qty')} @ {float(e.get('entry') or 0.0):.2f}" for e in entries]) if entries else "none"
    ex_txt = "; ".join([f"{e.get('symbol')} {e.get('side')} q={e.get('qty')} exit={float(e.get('exit') or 0.0):.2f} reason={e.get('reason')}" for e in exits]) if exits else "none"
    univ_changes = len(list(STATE.get("universe_changes_today") or []))
    route_changes = len(list(STATE.get("route_changes_today") or []))
    return (
        "📈 Analytics\n\n"
        f"Runtime: {runtime_ver}\n"
        f"Regime/Bias: {regime_now} / {bias}\n"
        f"Top3: {top3}\n"
        f"Active Universe: {active_uni}\n"
        f"Open Trades: {len(open_rows)}\n"
        f"Realized/Unrealized/Total: ₹{realized:.2f} / ₹{unrealized:.2f} / ₹{total:.2f}\n"
        f"Recent Entries: {len(entries)} | Recent Exits: {len(exits)}\n"
        f"Universe Changes Today: {univ_changes}\n"
        f"Route Changes Today: {route_changes}\n"
        f"Recent Entry Detail: {ent_txt}\n"
        f"Recent Exit Detail: {ex_txt}\n\n"
        f"Open Trade Snapshot: {('; '.join(open_rows[:6]) if open_rows else 'none')}"
    )


def get_strategy_selector_text() -> str:
    fams = list(STATE.get("active_strategy_families") or [])
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    scores = dict(STATE.get("strategy_scores_last") or {})
    lines = ["🧭 Strategy Selector", ""]
    lines.append(f"Active Top3: {','.join(fams) if fams else 'none'}")
    lines.append(f"Last Refresh Reason: {last_reason}")
    if scores:
        lines.append("")
        lines.append("Latest Family Scores")
        ranked = sorted(
            [(fam, int((meta or {}).get('score') or 0)) for fam, meta in scores.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        for fam, sc in ranked[:8]:
            lines.append(f"{fam}: {sc}")
    return "\n".join(lines)


def get_top3_text() -> str:
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    fams = list(STATE.get("active_strategy_families") or [])
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    last_refresh = float(STATE.get("active_strategy_last_refresh") or 0.0)
    last_refresh_txt = datetime.fromtimestamp(last_refresh, tz=IST).strftime("%H:%M:%S") if last_refresh > 0 else "n/a"
    return (
        "📌 Top 3 Strategies\n\n"
        f"Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Top3: {','.join(fams) if fams else 'none'}\n"
        f"Last Refresh: {last_refresh_txt}\n"
        f"Reason: {last_reason}"
    )


def get_strategy_scores_text() -> str:
    scores = dict(STATE.get("strategy_scores_last") or {})
    top3 = set([str(x).strip().lower() for x in list(STATE.get("active_strategy_families") or [])])
    if not scores:
        return "🧠 Strategy Scores\n\nNo scores available yet."
    rows = sorted(
        [(str(fam), int((meta or {}).get("score") or 0)) for fam, meta in scores.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    lines = ["🧠 Strategy Scores", ""]
    for fam, sc in rows:
        mark = "⭐" if fam.lower() in top3 else "•"
        lines.append(f"{mark} {fam}: {sc}")
    return "\n".join(lines[:20])


def get_regime_text() -> str:
    regime_now = str(STATE.get("last_regime") or "UNKNOWN")
    bias = str(get_regime_entry_mode(regime_now) or "UNKNOWN")
    opening_mode = str(STATE.get("opening_mode") or "OPEN_CLEAN")
    trend_direction = str(STATE.get("last_trend_direction") or "UNKNOWN")
    return (
        "🌐 Regime Snapshot\n\n"
        f"Regime: {regime_now}\n"
        f"Bias: {bias}\n"
        f"Opening Mode: {opening_mode}\n"
        f"Trend Direction: {trend_direction}"
    )


def get_route_status_text() -> str:
    fams = list(STATE.get("active_strategy_families") or [])
    route_source = str(STATE.get("last_route_universe_source") or "n/a")
    fallback_active = bool(STATE.get("fallback_mode_active"))
    dry_cycles = int(STATE.get("top3_dry_cycles") or 0)
    micro_active = bool(STATE.get("micro_mode_active"))
    last_reason = str(STATE.get("active_strategy_last_reason") or "n/a")
    return (
        "📊 Route Status\n\n"
        f"Active Top3: {','.join(fams) if fams else 'none'}\n"
        f"Universe Source: {route_source}\n"
        f"Fallback Active: {fallback_active}\n"
        f"Top3 Dry Cycles: {dry_cycles}\n"
        f"Micro Mode: {micro_active}\n"
        f"Last Recompute Reason: {last_reason}"
    )




def get_trailing_status_text():
    _ensure_day_key()
    rows = []
    for sym, t in sorted(_positions().items()):
        entry, qty = _trade_entry_qty(t)
        if entry <= 0:
            continue
        kite = None
        try:
            kite = get_kite()
        except Exception:
            kite = None
        ltp = _ltp(kite, sym) if kite else entry
        if ltp is None:
            ltp = entry

        value = entry * qty
        side = str((t or {}).get("side") or "LONG").upper()
        pnl_inr, _ = _calc_pnl(entry, ltp, qty, side=side)
        peak_pnl_inr = float(t.get("peak_pnl_inr") or 0.0)
        peak_pnl_inr = max(peak_pnl_inr, pnl_inr)

        tier = str(t.get("confidence_tier") or "FULL").upper()
        trade_product = str(t.get("product") or "MIS").upper()
        activate_inr = ee_calc_trail_activate_inr(entry, qty, tier=tier, side=side, product=trade_product)
        min_locked_pnl, allowed_giveback_inr = ee_dynamic_trail_levels(peak_pnl_inr, tier, product=trade_product)
        trigger_inr = max(min_locked_pnl, peak_pnl_inr - allowed_giveback_inr)
        trail_active = bool(t.get("trailing_active", t.get("trail_active", False)))

        rows.append(
            f"- {sym} qty={qty} entry={entry:.2f} ltp={ltp:.2f} value={value:.2f} "
            f"pnl_inr={pnl_inr:.2f} peak_pnl_inr={peak_pnl_inr:.2f} "
            f"trail_active={trail_active} tier={tier} activate_inr={activate_inr:.2f} "
            f"min_locked_pnl={min_locked_pnl:.2f} allowed_giveback_inr={allowed_giveback_inr:.2f} "
            f"trigger_inr={trigger_inr:.2f}"
        )

    if not rows:
        return "📉 Trailing Status\n\nNo open trades."

    return "📉 Trailing Status\n\n" + "\n".join(rows)




def _scan_short_entries(universe: list, max_new: int, strategy_family: str = "short_breakdown", universe_source: str = "primary") -> int:
    opened = 0
    held = set(_positions().keys())
    rejects = {}
    for sym in [s for s in universe if s not in held][: max_new * 3]:
        if opened >= max_new:
            break
        append_log("INFO", "SCAN", f"Scanning {sym}")
        sig = generate_short_signal(sym, strategy_family=strategy_family)
        if not sig:
            rej = str(STATE.get("last_short_reject_reasons", {}).get(sym) or "no_short_signal")
            rejects[rej] = int(rejects.get(rej) or 0) + 1
            SA.record_skipped_signal(
                {
                    "symbol": sym,
                    "side": "SHORT",
                    "reason": rej,
                    "strategy_tag": "short_breakdown",
                    "strategy_family": strategy_family,
                }
            )
            continue
        sig.setdefault("strategy_family", strategy_family or "short_breakdown")
        sig.setdefault("universe_source", universe_source or "primary")
        if _maybe_enter_short_from_signal(sig):
            opened += 1
    if rejects:
        summary = ",".join([f"{k}:{v}" for k, v in sorted(rejects.items(), key=lambda x: x[1], reverse=True)[:5]])
        append_log("INFO", "SCAN", f"short_reject_summary family={strategy_family} {summary}")
    return opened


def _maybe_send_eod_report():
    try:
        now = datetime.now(IST)
        hhmm = str(getattr(CFG, "EOD_REPORT_TIME", "15:16") or "15:16")
        hh, mm = [int(x) for x in hhmm.split(":", 1)]
        if now.hour < hh or (now.hour == hh and now.minute < mm):
            return
        day = now.strftime("%Y-%m-%d")
        if str(STATE.get("eod_report_sent_date") or "") == day:
            return
        if _open_positions_count() > 0:
            return
        append_log("INFO", "EOD", "Sending Telegram report")
        _notify(SA.generate_eod_report_text(STATE))
        STATE["eod_report_sent_date"] = day
    except Exception as e:
        append_log("WARN", "EOD", f"report generation failed: {e}")


def reconcile_broker_positions():
    if not is_live_enabled():
        return
    try:
        kite = get_kite()
        positions_resp = kite.positions() or {}
        net_positions = positions_resp.get("net") or []
        # Treat a completely empty API response as a suspect fetch — the broker
        # always returns a "net" key even when flat.  If it's missing entirely
        # the call likely failed or returned partial data, so bail out rather
        # than silently wiping local position tracking.
        if "net" not in positions_resp:
            append_log("WARN", "RECON", "broker positions response missing 'net' key — skipping reconcile to protect local state")
            return
    except Exception as e:
        append_log("WARN", "RECON", f"broker position fetch failed: {e}")
        return
    local = _positions()
    now_ts = datetime.now(IST).isoformat(timespec="seconds")
    broker_map = {}
    for p in net_positions:
        sym = str((p or {}).get("tradingsymbol") or "").strip().upper()
        qty = int((p or {}).get("quantity") or 0)
        if not sym or qty == 0:
            continue
        avg = float((p or {}).get("average_price") or 0.0)
        side = "BUY" if qty > 0 else "SHORT"
        # Capture product type from broker so reconciled positions get correct
        # MIS/CNC handling (force exit, trailing, stoploss widths).
        product = str((p or {}).get("product") or _get_product_for_mode()).upper()
        broker_map[sym] = {"qty": abs(qty), "avg": avg, "side": side, "product": product}
    # Only remove local positions if broker returned at least one position OR
    # we have confirmed the account is genuinely flat (local also has no positions).
    # This prevents a network glitch / empty response from wiping position memory.
    local_count = len(local)
    broker_count = len(broker_map)
    if local_count > 0 and broker_count == 0:
        append_log("WARN", "RECON", f"broker returned 0 positions but local has {local_count} — skipping removal to protect local state")
        return
    for sym, bp in broker_map.items():
        if sym in local:
            tr = dict(local.get(sym) or {})
            lqty = int(tr.get("qty") or tr.get("quantity") or 0)
            lside = str(tr.get("side") or "BUY").upper()
            if lqty != int(bp["qty"]) or lside != str(bp["side"]):
                PM.set(sym, {**tr, "qty": int(bp["qty"]), "quantity": int(bp["qty"]), "side": str(bp["side"])})
                append_log("INFO", "RECON", f"synced_broker_qty symbol={sym} local_qty={lqty} broker_qty={bp['qty']} local_side={lside} broker_side={bp['side']}")
            continue
        PM.set(sym, {
            "symbol": sym,
            "side": bp["side"],
            "entry": float(bp["avg"] or 0.0),
            "entry_price": float(bp["avg"] or 0.0),
            "qty": int(bp["qty"]),
            "quantity": int(bp["qty"]),
            "peak": 0.0,
            "peak_pct": 0.0,
            "peak_pnl_inr": 0.0,
            "trail_active": False,
            "trailing_active": False,
            "order_id": None,
            "product": bp.get("product", _get_product_for_mode()),
            "strategy_tag": "reconciled_external",
            "strategy_family": "reconciled_external",
            "confidence_tier": "RECON",
            "opening_mode": str(STATE.get("opening_mode") or "OPEN_CLEAN"),
            "market_regime": str((get_market_regime_snapshot() or {}).get("regime") or "UNKNOWN"),
            "universe_source": "broker_reconciled",
            "sector": _sector_for_symbol(sym),
            "entry_time": now_ts,
        })
        append_log("INFO", "RECON", f"synced_broker_open symbol={sym} side={bp['side']} qty={bp['qty']} entry={bp['avg']:.2f}")
        _log_trade_event("FILL", {**dict(local.get(sym) or {}), "symbol": sym})
    for sym in list(local.keys()):
        if sym not in broker_map and sym in _positions():
            append_log("WARN", "RECON", f"local_open_missing_on_broker symbol={sym}")
            tr = dict(_positions().get(sym) or {})
            PM.remove(sym)
            _log_trade_event("CLOSE", {**tr, "symbol": sym, "exit_reason": "RECON_BROKER_FLAT", "exit_time": now_ts})


def _scan_long_entries(universe: list, max_new: int, signal_fn=generate_signal, strategy_family: str = "trend_long", universe_source: str = "primary") -> int:
    before = _open_positions_count()

    def _signal_with_family(cands):
        sig = signal_fn(cands)
        if not sig and signal_fn is generate_signal:
            sig = generate_vwap_ema_signal(cands)
        if not sig and signal_fn is generate_signal:
            sig = generate_mean_reversion_signal(cands)
        if not sig and cands:
            reject_map = getattr(SE, "LAST_SIGNAL_REJECT_REASONS", {}) or {}
            seen = set()
            for raw_sym in cands:
                sym = str(raw_sym or "").strip().upper()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                rej = str(reject_map.get(sym) or "family_conditions_not_met")
                append_log("INFO", "SIG", f"family={str(strategy_family or 'trend_long')} symbol={sym} reject={rej}")
                SA.record_skipped_signal(
                    {
                        "symbol": sym,
                        "side": "BUY",
                        "reason": rej,
                        "strategy_tag": str(strategy_family or "trend_long"),
                        "strategy_family": str(strategy_family or "trend_long"),
                    }
                )
        if sig and strategy_family and not sig.get("strategy_family"):
            sig["strategy_family"] = strategy_family
        if sig and universe_source and not sig.get("universe_source"):
            sig["universe_source"] = universe_source
        if sig:
            append_log(
                "INFO",
                "SIG",
                f"family={str(sig.get('strategy_family') or strategy_family)} symbol={str(sig.get('symbol') or '').strip().upper()} setup={str(sig.get('strategy_setup') or sig.get('strategy_tag') or 'n/a')}",
            )
        return sig

    ee_process_entries(
        universe,
        _positions(),
        signal_fn=_signal_with_family,
        try_enter_fn=_maybe_enter_from_signal,
        max_new=max_new,
    )
    return max(0, _open_positions_count() - before)

def tick():
    _cfg_obj()
    _ensure_day_key()
    evaluate_ip_compliance(force=False)
    _sync_wallet_and_caps(force=False)
    reconcile_broker_positions()
    _refresh_runtime_pnl_fields()
    RISK.check_day_drawdown_guard(STATE)

    if _past_force_exit_time() and _positions():
        if not STATE.get("force_exit_done"):
            safe_set(STATE, "force_exit_done", True)
            # Only force-exit INTRADAY (MIS) positions. SWING/CNC positions
            # are held overnight — broker does NOT auto-square them.
            # Primary field = trade_mode (new). Fallback = product (legacy).
            def _is_intraday(t):
                tm = str(t.get("trade_mode") or "").upper()
                if tm in ("INTRADAY", "SWING"):
                    return tm == "INTRADAY"
                return str(t.get("product") or "MIS").upper() == "MIS"

            mis_positions = {s: t for s, t in _positions().items() if _is_intraday(t)}
            cnc_positions = {s: t for s, t in _positions().items() if not _is_intraday(t)}
            if mis_positions:
                append_log("WARN", "TIME", f"FORCE_EXIT triggered for {len(mis_positions)} INTRADAY/MIS positions")
                ee_force_exit_all(mis_positions, _close_position, reason="TIME")
            if cnc_positions:
                append_log("INFO", "TIME", f"SWING/CNC held overnight: {len(cnc_positions)} positions (force-exit skipped)")
            # Only pause if no swing positions remain — swing mode continues next day
            if not cnc_positions:
                safe_set(STATE, "paused", True)
            day_pnl = float(STATE.get("today_pnl") or 0.0)
            pnl_label = "Profit" if day_pnl >= 0 else "Loss"
            _notify(
                "🧾 Trading Day Brief\n"
                f"- MIS Closed (TIME): {len(mis_positions)}\n"
                f"- CNC Held Overnight: {len(cnc_positions)}\n"
                f"- Day {pnl_label}: ₹{abs(day_pnl):.2f}\n"
                f"- Net Day PnL: ₹{day_pnl:.2f}\n"
                f"- Open Positions Now: {_open_positions_count()}"
            )
            append_log("INFO", "TIME", "FORCE_EXIT completed for today")
        else:
            append_log("INFO", "TIME", "FORCE_EXIT already done today — skipping")


    # even when paused, force-exit check above still runs
    if STATE.get("paused"):
        return

    if STATE.get("halt_for_day"):
        append_log("WARN", "RISK", "halt_for_day active. Pausing loop.")
        safe_set(STATE, "paused", True)
        return

    if float(STATE.get("daily_loss_cap_inr") or 0.0) > 0 and STATE["today_pnl"] <= -abs(float(STATE["daily_loss_cap_inr"])):
        append_log("WARN", "CAP", "Daily loss cap hit. Pausing loop.")
        safe_set(STATE, "paused", True)
        return

    prof_milestone = float(STATE.get("daily_profit_milestone_inr") or 0.0)
    if prof_milestone > 0 and STATE["today_pnl"] >= prof_milestone and not STATE.get("profit_milestone_hit"):
        safe_set(STATE, "profit_milestone_hit", True)
        append_log("INFO", "CAP", f"Profit milestone hit at ₹{STATE['today_pnl']:.2f}")
        _notify(f"🎯 Profit milestone hit: ₹{STATE['today_pnl']:.2f}")
        if not bool(RUNTIME.get("SOFT_PROFIT_TARGET", True)):
            safe_set(STATE, "paused", True)
            return

    if bool(_cfg_get("AUTO_PROMOTE_ENABLED", False)) and not _positions() and _in_any_promote_window() and _cooldown_ok():
        if _market_stable():
            promote_universe(reason="AUTO_STABLE")

    ee_monitor_positions(
        STATE,
        _positions(),
        get_ltp=lambda sym: _ltp(get_kite(), sym),
        close_position=_close_position,
        force_exit_check=_past_force_exit_time,
    )

    if not _within_entry_window():
        return

    research_universe = _resolve_trade_universe()
    if not research_universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return
    _record_research_event("night_or_live_universe", f"resolved_size={len(research_universe)}")

    active_universe = refresh_active_universe_if_due(research_universe)
    if not active_universe:
        active_universe = list(research_universe[: int(_cfg_get("ACTIVE_UNIVERSE_SIZE", 8) or 8)])
        _record_universe_change("active_universe_fallback", "research_universe", active_universe, [], fallback_active=bool(STATE.get("fallback_mode_active")))

    max_new_cfg = int(os.getenv("MAX_NEW_ENTRIES_PER_TICK", "5"))
    max_concurrent = _dynamic_max_concurrent()
    remaining_slots = max(0, max_concurrent - _open_positions_count())
    max_new = max(0, min(max_new_cfg, remaining_slots))
    if max_new <= 0:
        append_log("INFO", "RISK", "max concurrent trade slots consumed; skipping new entries")
        return
    snap = get_market_regime_snapshot() or {}
    regime = str(snap.get("regime", "UNKNOWN") or "UNKNOWN").upper()
    trend_direction = str(snap.get("trend_direction", "UNKNOWN") or "UNKNOWN").upper()
    STATE["last_trend_direction"] = trend_direction
    regime_u = regime
    if regime == "TRENDING":
        if trend_direction == "DOWN":
            regime_u = "TRENDING_DOWN"
        elif trend_direction == "UP":
            regime_u = "TRENDING_UP"
    prev_regime = str(STATE.get("last_regime") or "")
    STATE["last_regime"] = regime_u
    append_log("INFO", "MARKET", f"regime={regime_u} entry_mode={get_regime_entry_mode(regime_u)} trend_direction={trend_direction}")
    if _is_micro_mode_active() and prev_regime and prev_regime != regime_u:
        _deactivate_micro_mode("regime_changed")

    open_mode, open_metrics = get_opening_mode()
    safe_set(STATE, "opening_mode", open_mode)
    safe_set(STATE, "opening_metrics", dict(open_metrics or {}))
    if _in_open_filter_window():
        reason = str((open_metrics or {}).get("reason") or "n/a")
        conf_i = int((open_metrics or {}).get("confidence") or 0)
        action = {
            "OPEN_CLEAN": "NORMAL_TRADING",
            "OPEN_MODERATE": "REDUCED_TRADING",
            "OPEN_UNSAFE": "MICRO_TRADING",
            "OPEN_FEED_RETRY": "WAIT_RETRY",
            "OPEN_HARD_BLOCK": "BLOCK_ALL",
        }.get(open_mode, "NORMAL_TRADING")
        append_log("INFO", "OPEN", f"state={open_mode} reason={reason} action={action} confidence={conf_i}")
        if open_mode == "OPEN_FEED_RETRY":
            time.sleep(20)
            return
        if open_mode == "OPEN_HARD_BLOCK":
            append_log("WARN", "OPEN", f"state=OPEN_HARD_BLOCK reason={reason} action=BLOCK_ALL")
            _deactivate_micro_mode(reason)
            return

    selected_families = _maybe_refresh_active_strategy_families(regime_u, trend_direction, active_universe, research_universe)
    if not selected_families:
        selected_families = ["mean_reversion"]
        append_log("WARN", "ROUTE", "[ROUTE] no strategy met min score -> using micro fallback family=mean_reversion")

    opened = 0
    append_log("INFO", "UNIV", "scanning active universe")
    opened += _scan_top3_families(active_universe, selected_families, max_new=max_new, universe_source="active_universe")

    research_tail = [s for s in research_universe if s not in set(active_universe)]
    if opened <= 0:
        safe_update(STATE, "active_no_setup_cycles", lambda v: int(v or 0) + 1)
        append_log("INFO", "UNIV", "no setup in active universe → scanning research universe")
        _record_research_event("route_change", "route_scan=research_universe", active_top3=selected_families)
        opened += _scan_top3_families(research_tail, selected_families, max_new=max_new - opened, universe_source="research_universe")

    if regime_u == "SIDEWAYS":
        if opened <= 0 and "mean_reversion" in selected_families:
            dry_cycles = safe_update(STATE, "mean_reversion_dry_cycles", lambda v: int(v or 0) + 1)
            if dry_cycles >= 2:
                next_ranked = [f for f in selected_families if f != "mean_reversion"]
                selected_families = next_ranked + ["mean_reversion"]
                append_log("INFO", "ROUTE", f"[ROUTE] family switch after dry cycles dry={dry_cycles} switched_to={','.join(next_ranked) if next_ranked else 'none'}")
                if next_ranked and opened < max_new:
                    append_log("INFO", "ROUTE", "SIDEWAYS mean_reversion dry -> activating second-ranked family in same cycle")
                    opened += _scan_top3_families(active_universe, next_ranked, max_new=max_new - opened, universe_source="sideways_dry_fallback")
        else:
            STATE["mean_reversion_dry_cycles"] = 0

    expand_cycles = int(_cfg_get("ACTIVE_UNIVERSE_EXPAND_CYCLES", 3) or 3)
    if opened <= 0 and int(STATE.get("active_no_setup_cycles") or 0) >= expand_cycles:
        append_log("INFO", "SCAN", "active universe weak → expanding scan scope")
        expanded = list(research_universe)
        _record_research_event("route_change", "route_scan=expanded_universe", active_top3=selected_families)
        opened += _scan_top3_families(expanded, selected_families, max_new=max_new - opened, universe_source="expanded_universe")

    if opened <= 0:
        safe_update(STATE, "no_entry_cycles", lambda v: int(v or 0) + 1)
        safe_update(STATE, "top3_dry_cycles", lambda v: int(v or 0) + 1)
    else:
        with STATE_LOCK:
            STATE["no_entry_cycles"] = 0
            STATE["top3_dry_cycles"] = 0
            STATE["active_no_setup_cycles"] = 0
            STATE["fallback_mode_active"] = False

    # Raised from 4 → 10 cycles: prevents reactive strategy family churn during brief
    # choppy periods. 4 cycles (~2 min) was causing constant recomputes.
    dry_thr = int(_cfg_get("TOP3_DRY_CYCLE_THRESHOLD", 10) or 10)
    if int(STATE.get("top3_dry_cycles") or 0) >= dry_thr:
        append_log("WARN", "HEALTH", f"[HEALTH] top3 dry for {dry_thr} cycles -> recomputing")
        selected_families = _refresh_active_strategy_families(
            "top3_dry",
            regime_u,
            trend_direction,
            active_universe,
            research_universe,
        )
        append_log("INFO", "HEALTH", "[HEALTH] top3 dry -> expanding to fallback universe")

    # Reduced from 4 → 2 cycles: fallback universe activates faster (60s vs 120s)
    # when the primary universe has no setups — less dead time.
    trigger_n = int(_cfg_get("FALLBACK_TRIGGER_CYCLES", 2) or 2)
    if STATE.get("no_entry_cycles", 0) >= trigger_n:
        if not STATE.get("fallback_mode_active"):
            append_log("INFO", "UNIV", f"no tradable setup in primary for {trigger_n} cycles → activating fallback universe")
            build_fallback_universe()
            safe_set(STATE, "fallback_mode_active", True)
            _record_research_event("fallback_activation", f"trigger_n={trigger_n}")

    if STATE.get("fallback_mode_active"):
        fb = list(STATE.get("fallback_universe") or [])
        if fb:
            append_log("INFO", "UNIV", f"scanning fallback universe size={len(fb)} strategy=TOP3")
            fb_opened = 0
            append_log("INFO", "ROUTE", "fallback_universe active -> top3 routing")
            _record_research_event("route_change", "route_scan=fallback_universe", active_top3=list(STATE.get("active_strategy_families") or selected_families))
            fb_opened += _scan_top3_families(fb, list(STATE.get("active_strategy_families") or selected_families), max_new=max_new, universe_source="fallback_universe")
            if fb_opened > 0:
                append_log(
                    "INFO",
                    "UNIV",
                    f"fallback entries allowed opened={fb_opened} size_multiplier={float(_cfg_get('FALLBACK_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
                )
                safe_set(STATE, "no_entry_cycles", 0)
            else:
                append_log("INFO", "UNIV", "fallback scanned but no eligible entries this cycle")

    _overfilter_health_check()
    _maybe_send_eod_report()


def run_loop_forever():
    _cfg_obj()
    evaluate_ip_compliance(force=True)
    append_log("INFO", "BOOT", f"runtime_version={str(_cfg_get('RUNTIME_VERSION', 'unknown') or 'unknown')}")
    kite_ver = _kite_client_version()
    supports_mp = False
    fallback_enabled = True
    try:
        supports_mp = _kite_supports_market_protection(get_kite())
        fallback_enabled = not supports_mp
    except Exception as e:
        append_log("WARN", "BOOT", f"kite_runtime_probe_failed={e}")
    append_log("INFO", "BOOT", f"kite_client_version={kite_ver}")
    append_log("INFO", "BOOT", f"market_protection_signature_support={supports_mp}")
    append_log("INFO", "BOOT", f"market_protection_http_fallback_enabled={fallback_enabled}")
    append_log("INFO", "LOOP", "Trading loop started")
    append_log(
        "INFO",
        "MARKET",
        f"weak mode config top_n={int(_cfg_get('WEAK_MARKET_TOP_N', 20) or 20)} "
        f"min_score={float(_cfg_get('WEAK_MARKET_MIN_SCORE', 0.75) or 0.75):.2f} "
        f"size_multiplier={float(_cfg_get('WEAK_MARKET_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
    )
    append_log(
        "INFO",
        "ROUTE",
        f"top3 selector min_score={int(_cfg_get('STRATEGY_MIN_ACTIVE_SCORE', 40) or 40)} "
        f"refresh_min={int(_cfg_get('STRATEGY_SELECTION_REFRESH_MINUTES', 10) or 10)} "
        f"dry_threshold={int(_cfg_get('TOP3_DRY_CYCLE_THRESHOLD', 5) or 5)}",
    )
    append_log(
        "INFO",
        "OPEN",
        f"adaptive opening filter enabled unsafe_mult={float(_cfg_get('OPEN_UNSAFE_SIZE_MULTIPLIER', 0.25) or 0.25):.2f} "
        f"moderate_mult={float(_cfg_get('OPEN_MODERATE_SIZE_MULTIPLIER', 0.5) or 0.5):.2f}",
    )
    _migrate_legacy_positions()
    _load_state_snapshot()
    # Force a live wallet sync at startup — ensures last_wallet reflects the actual
    # broker balance, not CAPITAL_INR fallback or a stale yesterday snapshot.
    try:
        _sync_wallet_and_caps(force=True)
        wallet_at_boot = float(STATE.get("wallet_net_inr") or 0.0)
        append_log("INFO", "BOOT", f"startup_wallet_sync wallet_net={wallet_at_boot:.2f}")
        if wallet_at_boot <= 0:
            append_log("CRITICAL", "BOOT", "wallet_net=0 after startup sync — all trades will use minimum bucket (500 INR). Check KITE_ACCESS_TOKEN and CAPITAL_INR.")
    except Exception as _e:
        append_log("WARN", "BOOT", f"startup_wallet_sync_failed={_e}")
        if float(STATE.get("wallet_net_inr") or 0.0) <= 0:
            append_log("CRITICAL", "BOOT", "wallet_net=0 and startup sync failed — trades will use minimum bucket until wallet syncs successfully.")
    if not _active_trade_universe():
        _load_research_universe_from_file()
    while True:
        try:
            tick()
            _save_state_snapshot()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(_cfg_get("TICK_SECONDS", 20)))
