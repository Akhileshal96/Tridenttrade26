"""Candle pattern detection and quality filters for entry signals.

This module implements the Tier-1 candle-based filters from the 2026-05-01 audit:
  1. Reversal-pattern veto — block entries against bullish/bearish reversal candles
  2. Volume confirmation — require entry-candle volume ≥ N× rolling average
  3. Fresh-candle settling guard — defer entries in the first N seconds of a new bar

Design principles:
  • All filters default to LOG-ONLY mode (CFG.CANDLE_FILTERS_LOG_ONLY=True).
    They emit `[CANDLE_VETO] would_have_blocked ...` lines but still allow
    the trade. After observing live data for a few sessions, flip to hard-block.
  • Each filter is individually toggleable via CFG.USE_*.
  • The module never raises — any fetch/parse failure returns "allow" and logs WARN.
  • Caches candle data with a short TTL to avoid hammering kite.historical_data.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Candle fetch with TTL cache
# ============================================================================

# Cache structure: { (symbol, interval): (ts_monotonic, candles_list) }
_CANDLE_CACHE: dict[tuple, tuple[float, list[dict]]] = {}


def _cache_ttl_sec() -> float:
    return float(getattr(CFG, "CANDLE_CACHE_TTL_SEC", 30) or 30)


def _interval_to_kite(interval: str) -> str:
    s = (interval or "").strip().lower()
    return {
        "1m": "minute", "1minute": "minute", "minute": "minute",
        "5m": "5minute", "5min": "5minute", "5minute": "5minute",
        "15m": "15minute", "15min": "15minute", "15minute": "15minute",
        "1h": "60minute", "60m": "60minute", "60minute": "60minute",
    }.get(s, s or "5minute")


def fetch_candles(symbol: str, interval: str = "5minute", lookback_minutes: int = 90) -> list[dict]:
    """Fetch recent candles for a symbol. Returns list of dicts with keys
    {date, open, high, low, close, volume}. Returns empty list on any failure.

    Caches results with TTL = CFG.CANDLE_CACHE_TTL_SEC (default 30s).
    """
    if not symbol:
        return []
    sym = str(symbol).strip().upper()
    kite_interval = _interval_to_kite(interval)
    cache_key = (sym, kite_interval)
    now_mono = time.monotonic()
    cached = _CANDLE_CACHE.get(cache_key)
    if cached and (now_mono - cached[0]) < _cache_ttl_sec():
        return cached[1]

    try:
        from broker_zerodha import get_kite
        from instrument_store import token_for_symbol
        kite = get_kite()
        if kite is None:
            return []
        token = token_for_symbol(sym)
        if not token:
            return []
        end = datetime.now(IST)
        start = end - timedelta(minutes=int(max(30, lookback_minutes)))
        data = kite.historical_data(token, start, end, kite_interval)
        candles = list(data or [])
        _CANDLE_CACHE[cache_key] = (now_mono, candles)
        return candles
    except Exception as exc:
        append_log("WARN", "CANDLE", f"fetch_candles {sym} {kite_interval} failed: {exc}")
        return []


def clear_cache() -> None:
    """Test helper / manual reset."""
    _CANDLE_CACHE.clear()


# ============================================================================
# Single-candle pattern detectors (operate on dict {open,high,low,close,volume})
# ============================================================================

def _body(c: dict) -> float:
    try:
        return float(c["close"]) - float(c["open"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _abs_body(c: dict) -> float:
    return abs(_body(c))


def _range(c: dict) -> float:
    try:
        return float(c["high"]) - float(c["low"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _upper_wick(c: dict) -> float:
    try:
        return float(c["high"]) - max(float(c["open"]), float(c["close"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _lower_wick(c: dict) -> float:
    try:
        return min(float(c["open"]), float(c["close"])) - float(c["low"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def is_doji(c: dict, body_pct: float = 0.10) -> bool:
    """True if candle body is < body_pct of its range (indecision)."""
    rng = _range(c)
    if rng <= 0:
        return False
    return _abs_body(c) <= rng * body_pct


def is_hammer(c: dict, wick_ratio: float = 2.0) -> bool:
    """Bullish reversal: long lower wick, small upper wick, close in upper half.

    Lower wick must be ≥ wick_ratio × body, upper wick must be small (≤ body),
    and the candle's close must be in the upper half of its range.
    """
    body = _abs_body(c)
    if body <= 0:
        return False
    rng = _range(c)
    if rng <= 0:
        return False
    lw = _lower_wick(c)
    uw = _upper_wick(c)
    if lw < body * wick_ratio:
        return False
    if uw > body:
        return False
    try:
        close_pos = (float(c["close"]) - float(c["low"])) / rng
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
    return close_pos >= 0.5


def is_shooting_star(c: dict, wick_ratio: float = 2.0) -> bool:
    """Bearish reversal: long upper wick, small lower wick, close in lower half.

    Upper wick ≥ wick_ratio × body, lower wick ≤ body, close in lower half of range.
    """
    body = _abs_body(c)
    if body <= 0:
        return False
    rng = _range(c)
    if rng <= 0:
        return False
    uw = _upper_wick(c)
    lw = _lower_wick(c)
    if uw < body * wick_ratio:
        return False
    if lw > body:
        return False
    try:
        close_pos = (float(c["close"]) - float(c["low"])) / rng
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
    return close_pos <= 0.5


def is_bullish_engulfing(prev: dict, curr: dict) -> bool:
    """Current green candle's body fully engulfs prior red candle's body."""
    if _body(prev) >= 0:
        return False  # prev must be bearish (red)
    if _body(curr) <= 0:
        return False  # curr must be bullish (green)
    try:
        return float(curr["open"]) <= float(prev["close"]) and float(curr["close"]) >= float(prev["open"])
    except (KeyError, TypeError, ValueError):
        return False


def is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    """Current red candle's body fully engulfs prior green candle's body."""
    if _body(prev) <= 0:
        return False
    if _body(curr) >= 0:
        return False
    try:
        return float(curr["open"]) >= float(prev["close"]) and float(curr["close"]) <= float(prev["open"])
    except (KeyError, TypeError, ValueError):
        return False


def is_inside_bar(prev: dict, curr: dict) -> bool:
    """Current candle's range is fully inside the prior candle's range (consolidation)."""
    try:
        return float(curr["high"]) <= float(prev["high"]) and float(curr["low"]) >= float(prev["low"])
    except (KeyError, TypeError, ValueError):
        return False


def classify_candle(curr: dict, prev: dict | None = None) -> dict[str, bool]:
    """Return all detected pattern flags for a candle (and optional prior one)."""
    flags = {
        "doji": is_doji(curr),
        "hammer": is_hammer(curr),
        "shooting_star": is_shooting_star(curr),
        "bullish": _body(curr) > 0,
        "bearish": _body(curr) < 0,
    }
    if prev:
        flags["bullish_engulfing"] = is_bullish_engulfing(prev, curr)
        flags["bearish_engulfing"] = is_bearish_engulfing(prev, curr)
        flags["inside_bar"] = is_inside_bar(prev, curr)
    else:
        flags["bullish_engulfing"] = False
        flags["bearish_engulfing"] = False
        flags["inside_bar"] = False
    return flags


# ============================================================================
# High-level filters used by entry path
# ============================================================================

def check_reversal_against_entry(candles: list[dict], side: str) -> tuple[bool, str]:
    """Return (block, reason). block=True if the latest closed candle is a
    reversal pattern AGAINST the proposed entry direction.

    side ∈ {"BUY","LONG","SHORT","SELL"} — case-insensitive.
    Falls back to ("allow", "no_data") if candles list is too short.
    """
    if not candles or len(candles) < 1:
        return False, "no_data"
    s = str(side or "").upper()
    is_short = s in ("SHORT", "SELL")
    curr = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else None
    flags = classify_candle(curr, prev)

    if is_short:
        # Short entries are blocked when the latest candle is a BULLISH reversal.
        if flags.get("hammer"):
            return True, "bullish_hammer"
        if flags.get("bullish_engulfing"):
            return True, "bullish_engulfing"
        if flags.get("doji"):
            return True, "doji_indecision"
    else:
        # Long entries are blocked when the latest candle is a BEARISH reversal.
        if flags.get("shooting_star"):
            return True, "bearish_shooting_star"
        if flags.get("bearish_engulfing"):
            return True, "bearish_engulfing"
        if flags.get("doji"):
            return True, "doji_indecision"

    # Inside bar at any time = consolidation, not edge.
    if flags.get("inside_bar"):
        return True, "inside_bar_consolidation"

    return False, "ok"


def check_volume_confirmation(candles: list[dict], multiplier: float = 1.5,
                              lookback: int = 20) -> tuple[bool, float]:
    """Return (confirms, ratio). confirms=True if latest candle's volume
    is ≥ multiplier × the rolling-average of prior `lookback` candles.

    A `confirms=False, ratio=0.0` is returned for missing/insufficient data
    — caller should treat that as "no confirmation" (block in strict mode,
    allow in log-only mode).
    """
    if not candles or len(candles) < (lookback + 1):
        return False, 0.0
    try:
        latest_vol = float(candles[-1].get("volume") or 0)
        prior = candles[-(lookback + 1):-1]
        vols = [float(c.get("volume") or 0) for c in prior]
        avg = sum(vols) / len(vols) if vols else 0.0
        if avg <= 0:
            return False, 0.0
        ratio = latest_vol / avg
        return ratio >= multiplier, ratio
    except Exception:
        return False, 0.0


def is_within_candle_settling_window(seconds: int = 60, interval_min: int = 5,
                                     now: datetime | None = None) -> bool:
    """True if we're within the first `seconds` after a fresh `interval_min`
    candle started — i.e., the candle is still forming and order action
    based on it is unreliable.

    Pure function: caller can pass `now` for testability.
    """
    if seconds <= 0:
        return False
    cur = now or datetime.now(IST)
    minutes_into_period = cur.minute % int(max(1, interval_min))
    seconds_into_candle = minutes_into_period * 60 + cur.second
    return seconds_into_candle < int(seconds)


# ============================================================================
# Orchestrator — single entry point used by trading_cycle entry functions
# ============================================================================

def check_candle_filters(symbol: str, side: str) -> tuple[bool, str, dict]:
    """Run all enabled candle filters for a proposed entry.

    Returns:
      (allow, reason, ctx)
        allow:  True if the entry is allowed under HARD-BLOCK semantics.
                In log-only mode, callers should always allow regardless.
        reason: short tag like "ok", "bullish_hammer", "vol_below_1.5x",
                "settling_window", or "no_data".
        ctx:    diagnostic dict (volume_ratio, candle_flags, etc.) for logging.

    NOTE: callers must consult CFG.CANDLE_FILTERS_LOG_ONLY themselves to decide
    whether to actually block — this function only computes the verdict.
    """
    ctx: dict[str, Any] = {"symbol": symbol, "side": side}
    if not bool(getattr(CFG, "USE_CANDLE_FILTERS", False)):
        ctx["filters_disabled"] = True
        return True, "filters_disabled", ctx

    interval = str(getattr(CFG, "CANDLE_PATTERN_INTERVAL", "5minute") or "5minute")
    interval_min = 5 if "5" in interval else 15 if "15" in interval else 1

    # 1. Fresh-candle settling guard (cheap — no API call)
    if bool(getattr(CFG, "USE_FRESH_CANDLE_GUARD", True)):
        guard_sec = int(getattr(CFG, "FRESH_CANDLE_GUARD_SEC", 60) or 60)
        if is_within_candle_settling_window(seconds=guard_sec, interval_min=interval_min):
            ctx["settling_seconds"] = guard_sec
            return False, "settling_window", ctx

    # 2 + 3 require candle data
    candles = fetch_candles(symbol, interval=interval, lookback_minutes=120)
    ctx["candles_fetched"] = len(candles)
    if not candles:
        return True, "no_candle_data", ctx  # fail-open

    # 2. Reversal pattern veto
    if bool(getattr(CFG, "USE_REVERSAL_CANDLE_VETO", True)):
        block, reason = check_reversal_against_entry(candles, side)
        if block:
            ctx["pattern_flags"] = classify_candle(
                candles[-1], candles[-2] if len(candles) >= 2 else None
            )
            return False, f"reversal_{reason}", ctx

    # 3. Volume confirmation
    if bool(getattr(CFG, "USE_VOLUME_CONFIRMATION", True)):
        mult = float(getattr(CFG, "VOLUME_CONFIRMATION_MULT", 1.5) or 1.5)
        confirms, ratio = check_volume_confirmation(candles, multiplier=mult)
        ctx["volume_ratio"] = round(ratio, 2)
        ctx["volume_threshold"] = mult
        if not confirms:
            return False, f"volume_below_{mult}x", ctx

    return True, "ok", ctx
