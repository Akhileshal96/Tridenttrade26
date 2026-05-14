"""Tests for the Pile-1 cleanup pass (audit 2026-05-15).

Covers:
  #1  HYBRID mode gated off (ENABLE_HYBRID_MODE=false) — _normalize_trading_mode
      collapses HYBRID -> INTRADAY at the single chokepoint.
  #3  Swing max-hold — _check_swing_max_hold force-closes CNC positions held
      longer than SWING_MAX_HOLD_DAYS; leaves fresh CNC and all MIS alone.
  #4  daily_loss_guard log spam — emits the WARN once per trip, not every tick.
  #5  pytest log isolation — log_store does not attach the file handler under pytest.

#2 (RELIANCE) was a log-label clarity fix only — covered by a source check.
#6 (recon drift) was investigated and required no code change.
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import trading_cycle as CYCLE
import risk_engine as RISK
import log_store

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# #1 — HYBRID mode gated off
# ============================================================================

def test_hybrid_collapses_to_intraday_when_disabled(monkeypatch):
    monkeypatch.setattr(CFG, "ENABLE_HYBRID_MODE", False, raising=False)
    assert CYCLE._normalize_trading_mode("HYBRID") == "INTRADAY"
    assert CYCLE._normalize_trading_mode("hybrid") == "INTRADAY"


def test_hybrid_allowed_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(CFG, "ENABLE_HYBRID_MODE", True, raising=False)
    assert CYCLE._normalize_trading_mode("HYBRID") == "HYBRID"


def test_intraday_and_swing_unaffected_by_hybrid_gate(monkeypatch):
    monkeypatch.setattr(CFG, "ENABLE_HYBRID_MODE", False, raising=False)
    assert CYCLE._normalize_trading_mode("INTRADAY") == "INTRADAY"
    assert CYCLE._normalize_trading_mode("SWING") == "SWING"


def test_set_trading_mode_hybrid_becomes_intraday_when_disabled(monkeypatch):
    monkeypatch.setattr(CFG, "ENABLE_HYBRID_MODE", False, raising=False)
    monkeypatch.setattr(CYCLE, "set_env_value", lambda *a, **k: None, raising=False)
    ok, norm = CYCLE.set_trading_mode("HYBRID")
    assert ok is True
    assert norm == "INTRADAY", "with HYBRID gated off, /mode hybrid must land on INTRADAY"


def test_classify_trade_mode_never_returns_swing_when_hybrid_disabled(monkeypatch):
    """Even with a perfect HYBRID-qualifying signal, a HYBRID-mode bot with
    the gate off classifies as INTRADAY (because current_trading_mode()
    routes through _normalize_trading_mode)."""
    monkeypatch.setattr(CFG, "ENABLE_HYBRID_MODE", False, raising=False)
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["trading_mode"] = "HYBRID"
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY", f"HYBRID gated off must yield INTRADAY, got {tm} ({reason})"


# ============================================================================
# #3 — Swing max-hold
# ============================================================================

def _reset_positions():
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["positions"] = {}


def _cnc_position(days_old: float):
    entry_dt = datetime.now(IST) - timedelta(days=days_old)
    return {
        "symbol": "HAL", "side": "BUY", "entry": 4681.0, "qty": 1,
        "product": "CNC", "confidence_tier": "RECON",
        "entry_time": entry_dt.isoformat(),
    }


def test_swing_max_hold_closes_stale_cnc(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SWING_MAX_HOLD", True, raising=False)
    monkeypatch.setattr(CFG, "SWING_MAX_HOLD_DAYS", 7, raising=False)
    _reset_positions()
    closed = []
    monkeypatch.setattr(CYCLE, "_close_position",
                        lambda sym, reason="?": closed.append((sym, reason)) or True)
    monkeypatch.setattr(CYCLE, "_notify", lambda *a, **k: None)
    CYCLE.STATE["positions"] = {"HAL": _cnc_position(days_old=8)}  # 8 > 7
    n = CYCLE._check_swing_max_hold()
    assert n == 1
    assert closed == [("HAL", "SWING_MAX_HOLD")]


def test_swing_max_hold_leaves_fresh_cnc_alone(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SWING_MAX_HOLD", True, raising=False)
    monkeypatch.setattr(CFG, "SWING_MAX_HOLD_DAYS", 7, raising=False)
    _reset_positions()
    closed = []
    monkeypatch.setattr(CYCLE, "_close_position",
                        lambda sym, reason="?": closed.append((sym, reason)) or True)
    monkeypatch.setattr(CYCLE, "_notify", lambda *a, **k: None)
    CYCLE.STATE["positions"] = {"HAL": _cnc_position(days_old=2)}  # 2 < 7
    n = CYCLE._check_swing_max_hold()
    assert n == 0
    assert closed == []


def test_swing_max_hold_ignores_mis_positions(monkeypatch):
    """MIS positions are intraday — never subject to the multi-day max-hold."""
    monkeypatch.setattr(CFG, "USE_SWING_MAX_HOLD", True, raising=False)
    monkeypatch.setattr(CFG, "SWING_MAX_HOLD_DAYS", 7, raising=False)
    _reset_positions()
    closed = []
    monkeypatch.setattr(CYCLE, "_close_position",
                        lambda sym, reason="?": closed.append((sym, reason)) or True)
    monkeypatch.setattr(CYCLE, "_notify", lambda *a, **k: None)
    old_mis = _cnc_position(days_old=30)
    old_mis["product"] = "MIS"  # very old, but MIS
    CYCLE.STATE["positions"] = {"HAL": old_mis}
    n = CYCLE._check_swing_max_hold()
    assert n == 0, "MIS positions must be ignored by the swing max-hold sweep"


def test_swing_max_hold_disabled_via_flag(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SWING_MAX_HOLD", False, raising=False)
    _reset_positions()
    closed = []
    monkeypatch.setattr(CYCLE, "_close_position",
                        lambda sym, reason="?": closed.append((sym, reason)) or True)
    CYCLE.STATE["positions"] = {"HAL": _cnc_position(days_old=30)}
    n = CYCLE._check_swing_max_hold()
    assert n == 0


# ============================================================================
# #4 — daily_loss_guard log-spam idempotency
# ============================================================================

class _LogCap:
    def __init__(self):
        self.records = []
    def __call__(self, level, tag, msg):
        self.records.append((level, tag, msg))


def test_daily_loss_guard_logs_once_not_every_tick(monkeypatch):
    cap = _LogCap()
    monkeypatch.setattr(RISK, "append_log", cap)
    state = {
        "today_pnl": -500.0,        # well past any sane cap
        "day_peak_pnl": 0.0,
        "daily_loss_cap_inr": 200.0,
    }
    # Simulate 5 ticks while the guard stays tripped
    for _ in range(5):
        RISK.check_day_drawdown_guard(state, risk_profile="STANDARD")
    guard_logs = [r for r in cap.records if "daily_loss_guard triggered" in r[2]]
    assert len(guard_logs) == 1, (
        f"daily_loss_guard must log once per trip, not per tick — got {len(guard_logs)}"
    )
    assert state.get("_daily_loss_guard_logged") is True


def test_daily_loss_guard_flag_reset_on_day_rollover_keys():
    """The reset is wired in _ensure_day_key — confirm the key it sets exists
    in source so the next day re-logs."""
    import inspect
    src = inspect.getsource(CYCLE._ensure_day_key)
    assert '_daily_loss_guard_logged' in src and 'False' in src


# ============================================================================
# #5 — pytest log isolation
# ============================================================================

def test_log_store_does_not_write_to_file_under_pytest():
    """Running under pytest, log_store must NOT have attached the rotating
    file handler — otherwise tests pollute the production trident.log."""
    assert log_store._UNDER_PYTEST is True, "pytest should be detected"
    assert log_store._LOG_TO_FILE is False, (
        "file handler must be disabled under pytest to protect the live log"
    )
    # No RotatingFileHandler should be attached to the logger
    import logging.handlers
    file_handlers = [
        h for h in log_store.logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers == [], (
        "RotatingFileHandler must not be attached under pytest"
    )


# ============================================================================
# #2 — RELIANCE log-label clarity (source check)
# ============================================================================

def test_buy_trigger_relabelled_to_candidate():
    """The misleading 'BUY trigger' log (which implied an entry that wasn't
    happening) was renamed to 'BUY candidate'.

    We check the actual log f-STRINGS, not the whole file — the explanatory
    comment legitimately references the old name to document the rename.
    The old log lines were:
        f"{sym} BUY trigger last=..."
        f"{sym} MR BUY trigger last=..."
    """
    src_path = os.path.join(os.path.dirname(__file__), "..", "strategy_engine.py")
    with open(src_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    # New label must be present
    assert "BUY candidate" in src
    # The old log-line f-strings must be gone (these patterns only appear in
    # the actual append_log calls, never in comments)
    assert "BUY trigger last=" not in src, "old 'BUY trigger last=' log line still present"
    assert "MR BUY trigger last=" not in src, "old 'MR BUY trigger last=' log line still present"
