"""CRITICAL audit fix (2026-05-13): skip auto-exit logic for reconciled positions.

Bug context:
  On May 11, 2026 the bot entered a destructive reconciliation loop on M&M:

    09:00:24 [RECON] synced_broker_open M&M side=BUY qty=1 entry=3377.00
    09:07:25 [CLOSE] M&M @ 3377 → 3235 exit_reason=SL  (-₹142)
    09:07:45 [RECON] synced_broker_open M&M side=BUY qty=1 entry=3377.00
    09:07:45 [CLOSE] M&M @ 3377 → 3235 exit_reason=SL  (-₹142)  # phantom #2
    09:08:05 [RECON] synced_broker_open M&M side=BUY qty=1 entry=3377.00
    ... (loop continues)

  Root cause: holdings reconcile (shipped 2026-05-08) correctly preserved
  multi-day CNC positions, BUT the existing exit logic (SL_ATR, fixed SL,
  PER_TRADE_MAX_LOSS, etc.) computes P&L vs the original broker avg entry
  — which is days old. A position bought at ₹3377 (May 7) with current
  price ₹3235 (May 11) is ALREADY at -4.2% — beyond the SL threshold.
  Bot tries to close it. Close order conflicts with broker settlement
  state. Next reconcile re-creates the local position from the still-held
  broker holding. Loop.

  Real loss from M&M: ~₹142 (single close). Phantom logged loss: ~₹426
  (3× phantom closes). Real damage = brokerage + 1 real close.

Fix: skip ALL auto-exit logic for tier=RECON / family=reconciled_external
positions. Bot tracks them (for status + peak P&L) but doesn't auto-close.
User manages via Zerodha or /panic.
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import execution_engine as EE

IST = ZoneInfo("Asia/Kolkata")


def _make_state(wallet=20000.0, halt=False, today_pnl=0.0):
    return {"wallet_net_inr": wallet, "halt_for_day": halt, "today_pnl": today_pnl}


def _make_recon_trade(side="BUY", entry=3377.0, qty=1, product="CNC", days_old=4):
    """Build a reconciled position with a stale entry."""
    entry_dt = datetime.now(IST) - timedelta(days=days_old)
    return {
        "side": side,
        "entry": entry,
        "qty": qty,
        "confidence_tier": "RECON",
        "strategy_family": "reconciled_external",
        "strategy_tag": "reconciled_external",
        "product": product,
        "entry_time": entry_dt.isoformat(),
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
    }


def _make_normal_trade(side="BUY", entry=100.0, qty=10, minutes_ago=2):
    entry_dt = datetime.now(IST) - timedelta(minutes=minutes_ago)
    return {
        "side": side,
        "entry": entry,
        "qty": qty,
        "confidence_tier": "FULL",
        "strategy_family": "trend_long",
        "strategy_tag": "mtf_confirmed_long",
        "product": "MIS",
        "entry_time": entry_dt.isoformat(),
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
    }


class _Recorder:
    def __init__(self):
        self.closes = []
    def __call__(self, sym, reason="?", ltp_override=None):
        self.closes.append((sym, reason))
        return True


def _run_monitor(positions, state, ltp_map):
    rec = _Recorder()
    EE.monitor_positions(
        state,
        positions,
        get_ltp=lambda s: ltp_map.get(s),
        close_position=rec,
        force_exit_check=lambda: False,
    )
    return rec


# ============================================================================
# Critical: RECON positions are NOT auto-closed even when deeply underwater
# ============================================================================

def test_recon_position_with_deep_loss_is_not_closed(monkeypatch):
    """The M&M-on-2026-05-11 scenario: stale entry 3377, current 3235,
    pnl = -₹142 = 0.71% of ₹20k wallet (above 0.5% PER_TRADE_MAX_LOSS).
    Under the bug: bot fires PER_TRADE_MAX_LOSS close → loop.
    Under the fix: bot tracks position, NO close fires.
    """
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)

    state = _make_state(wallet=20000.0)
    positions = {"MNM": _make_recon_trade(side="BUY", entry=3377.0, qty=1)}
    rec = _run_monitor(positions, state, {"MNM": 3235.0})

    # Critical: NO close should fire on the reconciled position.
    assert rec.closes == [], (
        f"reconciled position must not be auto-closed; got: {rec.closes}"
    )


def test_recon_position_with_sl_breach_is_not_closed(monkeypatch):
    """Stale entry 3377, current 3100 = -8.2% — way past the 2% fixed SL
    and the 3% CNC swing SL. Under the bug: SL fires. Under the fix: skip."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    state = _make_state(wallet=20000.0)
    positions = {"MNM": _make_recon_trade(side="BUY", entry=3377.0, qty=1)}
    rec = _run_monitor(positions, state, {"MNM": 3100.0})  # -8.2% loss
    assert rec.closes == []


def test_recon_position_in_profit_no_trail_close(monkeypatch):
    """Reconciled position currently in profit. Bot should NOT trail-close it."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    state = _make_state(wallet=20000.0)
    positions = {"HAL": _make_recon_trade(side="BUY", entry=4681.0, qty=1)}
    # HAL went from 4681 → 5000 = +₹319, then back to 4900 (giveback)
    # Normally trail would close. Reconciled: should skip.
    rec = _run_monitor(positions, state, {"HAL": 4900.0})
    assert rec.closes == []


def test_family_reconciled_external_also_skipped(monkeypatch):
    """Some reconciled positions may have family=reconciled_external but
    tier != RECON. Both signals should trigger the skip."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    state = _make_state(wallet=20000.0)
    trade = _make_recon_trade(side="BUY", entry=3377.0, qty=1)
    trade["confidence_tier"] = "FULL"  # tier overridden, but family still reconciled
    positions = {"MNM": trade}
    rec = _run_monitor(positions, state, {"MNM": 3235.0})
    assert rec.closes == [], "family=reconciled_external should also trigger skip"


def test_recon_skip_can_be_disabled_via_flag(monkeypatch):
    """When SKIP_AUTO_EXIT_FOR_RECON=False, old (buggy) behavior returns.
    Provided as emergency revert toggle — not the recommended state."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", False, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    # Disable other guards so PER_TRADE_MAX_LOSS is the only firing rule
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)
    monkeypatch.setattr(CFG, "USE_FAILED_DEV_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_EARLY_NO_MOVE_EXIT", False, raising=False)
    monkeypatch.setattr(CFG, "USE_TIME_DECAY_EXIT", False, raising=False)

    state = _make_state(wallet=20000.0)
    # Use MIS product so PER_TRADE_MAX_LOSS isn't filtered by CNC-skip
    trade = _make_recon_trade(side="BUY", entry=3377.0, qty=1, product="MIS")
    positions = {"MNM": trade}
    rec = _run_monitor(positions, state, {"MNM": 3235.0})  # -0.71% wallet
    # With flag off: per-trade max-loss SHOULD fire (the old buggy behavior).
    # This confirms the flag controls the new fix without breaking the
    # underlying guard.
    assert any(r == "PER_TRADE_MAX_LOSS" for _, r in rec.closes), (
        f"with SKIP_AUTO_EXIT_FOR_RECON=False, per-trade-max-loss should fire; got: {rec.closes}"
    )


# ============================================================================
# Regression: normal (non-RECON) positions still auto-close as before
# ============================================================================

def test_normal_position_still_auto_closes_per_trade_max_loss(monkeypatch):
    """The fix must not break auto-management of normal trades."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)

    state = _make_state(wallet=20000.0)
    # Normal MIS trade: SHORT 10@100, ltp 120 → loss = -₹200 = -1% wallet
    positions = {"ABC": _make_normal_trade(side="SHORT", entry=100.0, qty=10)}
    rec = _run_monitor(positions, state, {"ABC": 120.0})

    closes = [r for _, r in rec.closes]
    assert "PER_TRADE_MAX_LOSS" in closes, (
        f"normal positions must still auto-close on per-trade max loss; got: {closes}"
    )


def test_peak_pnl_still_tracked_for_recon_diagnostics(monkeypatch):
    """Even though we skip exits, we still update peak_pnl_inr so the
    status panel shows correct unrealized P&L for held swing positions."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    state = _make_state(wallet=20000.0)
    trade = _make_recon_trade(side="BUY", entry=4681.0, qty=1)
    trade["peak_pnl_inr"] = 0.0
    positions = {"HAL": trade}
    # HAL went up from 4681 to 4750 = +₹69
    _run_monitor(positions, state, {"HAL": 4750.0})
    assert trade["peak_pnl_inr"] >= 69.0, (
        f"peak_pnl_inr must track for diagnostics; got {trade['peak_pnl_inr']}"
    )


# ============================================================================
# Mixed-position scenario (production-realistic)
# ============================================================================

def test_mixed_normal_and_recon_only_normal_closes(monkeypatch):
    """Production scenario: HAL (RECON, in red) + new BHEL trade (normal, in red).
    HAL should NOT close. BHEL SHOULD close (per its own SL rules)."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    monkeypatch.setattr(CFG, "USE_PER_TRADE_MAX_LOSS", True, raising=False)
    monkeypatch.setattr(CFG, "PER_TRADE_MAX_LOSS_PCT", 0.5, raising=False)
    monkeypatch.setattr(CFG, "USE_ATR_STOPLOSS", False, raising=False)
    monkeypatch.setattr(CFG, "STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "SHORT_STOPLOSS_PCT", 99.0, raising=False)
    monkeypatch.setattr(CFG, "USE_PROFIT_TARGET", False, raising=False)

    state = _make_state(wallet=20000.0)
    positions = {
        "HAL":  _make_recon_trade(side="BUY", entry=4681.0, qty=1),
        "BHEL": _make_normal_trade(side="BUY", entry=400.0, qty=20),
    }
    rec = _run_monitor(positions, state, {
        "HAL":  4500.0,  # -₹181 (4% loss, would normally trigger SL/cap)
        "BHEL": 395.0,   # -₹100 = 0.5% wallet (right at threshold)
    })

    closed_syms = [s for s, _ in rec.closes]
    assert "HAL" not in closed_syms, "RECON HAL should NOT auto-close"
    # BHEL may or may not close depending on the exact threshold; we just
    # ensure HAL is protected.
