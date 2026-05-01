"""Tests for the 4 audit-driven risk guards added on 2026-05-01:

  Fix #1: HALT_LOSER_FORCE_CLOSE — halt-for-day force-closes losers > X% wallet
  Fix #2: PER_TRADE_MAX_LOSS    — single-trade hard floor at X% wallet
  Fix #3: EARLY_NO_MOVE         — 5-min no-move bail (faster than FAILED_DEV)
  Fix #8: DAILY_DRAWDOWN_KILL   — %-wallet kill-switch + force-close intraday

These guards are independent of strategy/regime/profile and complement existing
risk controls (ATR SL, fixed SL, profit target, trail, FAILED_DEV, time decay,
loss-streak halt, daily INR loss cap).
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import execution_engine as EE
import trading_cycle as CYCLE

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Helpers
# ============================================================================

def _make_state(wallet=10000.0, halt=False, today_pnl=0.0):
    """Build a minimal state dict for monitor_positions."""
    return {
        "wallet_net_inr": wallet,
        "halt_for_day": halt,
        "today_pnl": today_pnl,
    }


def _make_trade(side="SHORT", entry=100.0, qty=10, minutes_ago=10, peak_pnl=0.0,
                tier="FULL", product="MIS"):
    """Build a position dict matching the live STATE shape."""
    entry_dt = datetime.now(IST) - timedelta(minutes=minutes_ago)
    return {
        "side": side,
        "entry": entry,
        "qty": qty,
        "confidence_tier": tier,
        "product": product,
        "entry_time": entry_dt.isoformat(),
        "peak_pnl_inr": peak_pnl,
        "trail_active": False,
        "trailing_active": False,
    }


class _Recorder:
    """Captures close_position calls (sym, reason)."""
    def __init__(self):
        self.calls = []
    def __call__(self, sym, reason="?", ltp_override=None):
        self.calls.append((sym, reason))
        return True


def _run_monitor(positions, state, ltp_map, force_exit=False):
    rec = _Recorder()
    EE.monitor_positions(
        state,
        positions,
        get_ltp=lambda s: ltp_map.get(s),
        close_position=rec,
        force_exit_check=lambda: force_exit,
    )
    return rec


# ============================================================================
# Fix #2: PER_TRADE_MAX_LOSS — single-trade hard floor
# ============================================================================

def test_per_trade_max_loss_fires_when_loss_exceeds_pct(monkeypatch):
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    # Disable other SL paths so PER_TRADE_MAX_LOSS is the only firing rule.
    # In production, fixed SL fires FIRST at 1.2% — only when SL is wider (e.g.
    # ATR with high vol stocks) does PER_TRADE_MAX_LOSS become the operative cap.
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    state = _make_state(wallet=10000.0)
    # Wallet 10k × 0.5% = ₹50 cap. Build a SHORT 10@100, ltp 110 → -10×10 = -₹100 loss.
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 110.0})
    assert ("ABC", "PER_TRADE_MAX_LOSS") in rec.calls, f"got: {rec.calls}"


def test_per_trade_max_loss_does_not_fire_within_cap(monkeypatch):
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)  # disable fixed SL
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)
    state = _make_state(wallet=10000.0)
    # Loss = 1×10 = ₹10, well under ₹50 cap
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 101.0})
    closed_reasons = [r for _, r in rec.calls]
    assert "PER_TRADE_MAX_LOSS" not in closed_reasons


def test_per_trade_max_loss_skipped_for_cnc(monkeypatch):
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    state = _make_state(wallet=10000.0)
    # CNC swing: should NOT trigger per-trade max-loss (overnight gap tolerated)
    positions = {"XYZ": _make_trade(side="SHORT", entry=100.0, qty=10, product="CNC")}
    rec = _run_monitor(positions, state, {"XYZ": 200.0})  # huge loss
    assert ("XYZ", "PER_TRADE_MAX_LOSS") not in rec.calls


def test_per_trade_max_loss_disabled_when_pct_zero(monkeypatch):
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.0, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    state = _make_state(wallet=10000.0)
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 200.0})
    assert ("ABC", "PER_TRADE_MAX_LOSS") not in rec.calls


# ============================================================================
# Fix #1: HALT_LOSER_FORCE_CLOSE — halt-for-day actively manages losers
# ============================================================================

def test_halt_loser_force_close_fires_when_halted_and_loss_exceeds(monkeypatch):
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", True, raising=False)
    monkeypatch.setattr(CFG, "HALT_LOSER_FORCE_CLOSE_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    # Disable other SL paths to isolate the halt-loser guard.
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    state = _make_state(wallet=10000.0, halt=True)
    # Wallet 10k × 0.5% = ₹50. Build a SHORT 10@100, ltp 106 → -₹60 loss.
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 106.0})
    assert ("ABC", "HALT_LOSER_CLOSE") in rec.calls, f"got: {rec.calls}"


def test_halt_loser_does_not_fire_without_halt(monkeypatch):
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", True, raising=False)
    monkeypatch.setattr(CFG, "HALT_LOSER_FORCE_CLOSE_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)
    state = _make_state(wallet=10000.0, halt=False)  # NOT halted
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 106.0})
    closed_reasons = [r for _, r in rec.calls]
    assert "HALT_LOSER_CLOSE" not in closed_reasons


# ============================================================================
# Fix #3: EARLY_NO_MOVE — 5-min no-move bail
# ============================================================================

def test_early_no_move_fires_when_dead_on_arrival(monkeypatch):
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", True, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_MINUTES", 5, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_PEAK_RATIO", 0.10, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", False, raising=False)
    state = _make_state(wallet=10000.0)
    # 8 min elapsed, peak = 0, current loss = -1×10 = -₹10 (small)
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10, minutes_ago=8, peak_pnl=0.0)}
    rec = _run_monitor(positions, state, {"ABC": 101.0})
    assert ("ABC", "EARLY_NO_MOVE") in rec.calls, f"got: {rec.calls}"


def test_early_no_move_does_not_fire_before_window(monkeypatch):
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", True, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_MINUTES", 5, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_PEAK_RATIO", 0.10, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", False, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)
    state = _make_state(wallet=10000.0)
    # Only 2 min in — too early
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10, minutes_ago=2)}
    rec = _run_monitor(positions, state, {"ABC": 101.0})
    closed_reasons = [r for _, r in rec.calls]
    assert "EARLY_NO_MOVE" not in closed_reasons


def test_early_no_move_does_not_fire_when_in_profit(monkeypatch):
    """If pnl_inr is positive, the trade IS moving — don't bail."""
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", True, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_MINUTES", 5, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_PEAK_RATIO", 0.10, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", False, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)
    state = _make_state(wallet=10000.0)
    # 8 min in, currently in profit (ltp 99 → SHORT profit +₹10)
    positions = {"ABC": _make_trade(side="SHORT", entry=100.0, qty=10, minutes_ago=8)}
    rec = _run_monitor(positions, state, {"ABC": 99.0})
    closed_reasons = [r for _, r in rec.calls]
    assert "EARLY_NO_MOVE" not in closed_reasons


def test_early_no_move_skipped_when_trail_active(monkeypatch):
    """A trade with active trail has clearly developed — never EARLY_NO_MOVE it."""
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", True, raising=False)
    monkeypatch.setattr(CFG, "EARLY_NO_MOVE_MINUTES", 5, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", False, raising=False)
    monkeypatch.setattr(CFG, "USE_HALT_LOSER_FORCE_CLOSE", False, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)
    state = _make_state(wallet=10000.0)
    trade = _make_trade(side="SHORT", entry=100.0, qty=10, minutes_ago=8, peak_pnl=0.0)
    trade["trail_active"] = True
    trade["trailing_active"] = True
    positions = {"ABC": trade}
    rec = _run_monitor(positions, state, {"ABC": 101.0})
    closed_reasons = [r for _, r in rec.calls]
    assert "EARLY_NO_MOVE" not in closed_reasons


# ============================================================================
# Fix #8: DAILY_DRAWDOWN_KILL — %-wallet kill-switch
# ============================================================================

def _reset_kill_state():
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["wallet_net_inr"] = 10000.0
        CYCLE.STATE["today_pnl"] = 0.0
        CYCLE.STATE["halt_for_day"] = False
        CYCLE.STATE["daily_drawdown_kill_fired"] = False
        CYCLE.STATE["positions"] = {}


def test_daily_drawdown_kill_fires_at_threshold(monkeypatch):
    monkeypatch.setattr(CFG, "USE_DAILY_DRAWDOWN_KILL", True, raising=False)
    monkeypatch.setattr(CFG, "DAILY_DRAWDOWN_KILL_PCT", 2.0, raising=False)
    _reset_kill_state()
    # Wallet 10k × 2% = ₹200 trigger. Set today_pnl to -250.
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["today_pnl"] = -250.0
    fired = CYCLE._check_daily_drawdown_kill_switch()
    assert fired is True
    assert CYCLE.STATE["halt_for_day"] is True
    assert CYCLE.STATE["daily_drawdown_kill_fired"] is True
    assert CYCLE.STATE.get("day_guard_reason") == "daily_drawdown_kill"


def test_daily_drawdown_kill_does_not_fire_within_threshold(monkeypatch):
    monkeypatch.setattr(CFG, "USE_DAILY_DRAWDOWN_KILL", True, raising=False)
    monkeypatch.setattr(CFG, "DAILY_DRAWDOWN_KILL_PCT", 2.0, raising=False)
    _reset_kill_state()
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["today_pnl"] = -100.0  # only 1% wallet
    fired = CYCLE._check_daily_drawdown_kill_switch()
    assert fired is False
    assert CYCLE.STATE.get("halt_for_day", False) is False
    assert CYCLE.STATE.get("daily_drawdown_kill_fired", False) is False


def test_daily_drawdown_kill_disabled_via_flag(monkeypatch):
    monkeypatch.setattr(CFG, "USE_DAILY_DRAWDOWN_KILL", False, raising=False)
    _reset_kill_state()
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["today_pnl"] = -1000.0  # huge loss
    fired = CYCLE._check_daily_drawdown_kill_switch()
    assert fired is False
    assert CYCLE.STATE.get("halt_for_day", False) is False


def test_daily_drawdown_kill_force_closes_intraday_only(monkeypatch):
    monkeypatch.setattr(CFG, "USE_DAILY_DRAWDOWN_KILL", True, raising=False)
    monkeypatch.setattr(CFG, "DAILY_DRAWDOWN_KILL_PCT", 2.0, raising=False)
    _reset_kill_state()

    closed = []
    def fake_force_exit(positions, close_fn, reason="?"):
        for s in positions:
            closed.append((s, reason))
        return True
    monkeypatch.setattr(CYCLE, "ee_force_exit_all", fake_force_exit)

    with CYCLE.STATE_LOCK:
        CYCLE.STATE["today_pnl"] = -250.0
        CYCLE.STATE["positions"] = {
            "INTRA1": {"trade_mode": "INTRADAY", "product": "MIS", "side": "BUY", "entry": 100, "qty": 1},
            "SWING1": {"trade_mode": "SWING", "product": "CNC", "side": "BUY", "entry": 200, "qty": 1},
        }

    fired = CYCLE._check_daily_drawdown_kill_switch()
    assert fired is True
    closed_syms = {s for s, _ in closed}
    assert closed_syms == {"INTRA1"}, f"only intraday should close; got {closed_syms}"


def test_daily_drawdown_kill_idempotent_across_ticks(monkeypatch):
    """Once fired today, subsequent ticks must not re-fire force-close."""
    monkeypatch.setattr(CFG, "USE_DAILY_DRAWDOWN_KILL", True, raising=False)
    monkeypatch.setattr(CFG, "DAILY_DRAWDOWN_KILL_PCT", 2.0, raising=False)
    _reset_kill_state()
    fire_count = {"n": 0}
    def fake_force_exit(positions, close_fn, reason="?"):
        fire_count["n"] += 1
        return True
    monkeypatch.setattr(CYCLE, "ee_force_exit_all", fake_force_exit)
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["today_pnl"] = -250.0
        CYCLE.STATE["positions"] = {"X": {"product": "MIS", "side": "BUY", "entry": 100, "qty": 1}}
    # Tick 1
    CYCLE._check_daily_drawdown_kill_switch()
    # Tick 2 — even with same conditions, force-close must NOT re-fire
    CYCLE._check_daily_drawdown_kill_switch()
    assert fire_count["n"] == 1, f"force-exit called {fire_count['n']} times; should be 1"


def test_daily_drawdown_kill_persisted_across_restart():
    """The kill flag must be in _STATE_PERSIST_KEYS so restart doesn't unhalt."""
    assert "daily_drawdown_kill_fired" in CYCLE._STATE_PERSIST_KEYS
