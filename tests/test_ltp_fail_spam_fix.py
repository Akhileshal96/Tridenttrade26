"""Audit fix (2026-05-16): bound the LTP-fail emergency-close path.

Bug context:
  After Friday May 15 EOD, the bot held HAL (CNC RECON position) and ran
  through midnight rollover. The Zerodha access token expired ~04:30 IST
  (before the 08:15 TOTP renewal window). For 3h 44m the bot:

    1. Failed to fetch HAL LTP every 20s tick (auth dead).
    2. After 3 fails, fired emergency_close on HAL.
    3. emergency_close placed a BUY CNC order (HAL stored side="SHORT").
    4. Broker rejected the order ("Incorrect api_key or access_token").
    5. _close_position returned False — position stayed in state.
    6. Next tick: fails=4, threshold tripped again, repeated.

  Total damage in trident2.log (2026-05-15 → 2026-05-16):
    * 619 [LTP] consecutive_fails warnings
    * 615 emergency_close triggers
    * 1,845 BUY order attempts (3 retries × 615 cycles)
    * 1,845 broker rejections (ERROR level)
    * Only stopped by 08:15 TOTP renewal — token expiry was the safety net

  If the token had been valid (or if it next expires inside market hours
  for a real reason), some of those 1,845 BUY orders WOULD have landed.
  For a position stored as SHORT being "closed" via BUY, that's catastrophic.

Fixes in execution_engine.monitor_positions:
  A. RECON guard moved BEFORE the LTP-unavailable handler. RECON positions
     should never auto-close — period — including not for LTP outages.
  B. LTP-fail emergency-close only fires during market hours (09:15-15:30).
     Pre/post-market LTP gaps are predictable, not emergencies.
  C. emergency_close fires ONCE per session per symbol via the
     `_emergency_close_fired` flag, not every cycle past fails>=3.
     Flag is cleared on next successful LTP fetch (re-arm for future).

Companion fixes (separate test files):
  * trading_cycle._place_live_order: fast-fail on auth errors.
  * trading_cycle._load_state_snapshot: normalize RECON side to BUY (CNC
    holdings cannot be SHORT).
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import execution_engine as EE

IST = ZoneInfo("Asia/Kolkata")


# ----------------------------------------------------------------------------
# Helpers (mirrors test_recon_auto_exit_fix style)
# ----------------------------------------------------------------------------

def _state(wallet=20000.0, halt=False):
    return {"wallet_net_inr": wallet, "halt_for_day": halt, "today_pnl": 0.0}


def _recon_trade(side="BUY", entry=4681.0, qty=1, product="CNC", days_old=4):
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


def _normal_trade(side="BUY", entry=100.0, qty=10, minutes_ago=2, product="MIS"):
    entry_dt = datetime.now(IST) - timedelta(minutes=minutes_ago)
    return {
        "side": side,
        "entry": entry,
        "qty": qty,
        "confidence_tier": "FULL",
        "strategy_family": "trend_long",
        "strategy_tag": "mtf_confirmed_long",
        "product": product,
        "entry_time": entry_dt.isoformat(),
        "peak_pnl_inr": 0.0,
        "trail_active": False,
        "trailing_active": False,
    }


class _Recorder:
    def __init__(self):
        self.closes = []

    def __call__(self, sym, reason="?", ltp_override=None):
        self.closes.append((sym, reason, ltp_override))
        return True  # Pretend the close succeeded — Fix C still applies anyway.


def _run_with_dead_ltp(positions, state, n_ticks=10, ltp_alive_for=None,
                       in_market_hours=True):
    """Run monitor_positions n_ticks times with LTP returning None each tick.

    ltp_alive_for: set of symbols whose LTP IS available (returns 100.0).
    in_market_hours: if False, _is_market_hours() returns False (simulates the
                     pre-market scenario that produced Friday's HAL spam).
    """
    ltp_alive_for = ltp_alive_for or set()
    rec = _Recorder()
    # Patch _is_market_hours directly — patching datetime breaks the .replace()
    # chain inside the real helper.
    with patch.object(EE, "_is_market_hours", return_value=in_market_hours):
        for _ in range(n_ticks):
            EE.monitor_positions(
                state,
                positions,
                get_ltp=lambda s: 100.0 if s in ltp_alive_for else None,
                close_position=rec,
                force_exit_check=lambda: False,
            )
    return rec


# ============================================================================
# Fix A — RECON positions never trigger emergency-close on LTP outage
# ============================================================================

def test_recon_ltp_unavailable_never_fires_emergency_close(monkeypatch):
    """The Friday HAL scenario: RECON position + dead LTP feed.
    Even after 100 ticks of LTP=None, NO emergency close should fire.
    """
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"HAL": _recon_trade(side="BUY", entry=4681.0, qty=1)}
    rec = _run_with_dead_ltp(
        positions, _state(), n_ticks=100,
        in_market_hours=True,
    )
    assert rec.closes == [], (
        f"RECON position must not fire emergency_close even after 100 ticks "
        f"of LTP failure; got: {rec.closes}"
    )


def test_recon_ltp_unavailable_does_not_increment_fail_counter(monkeypatch):
    """RECON skip is silent — no `_ltp_fail_*` accumulation either.
    Otherwise the trade dict bloats with thousands of fail counters
    overnight (one per symbol per tick gap)."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"HAL": _recon_trade(side="BUY", entry=4681.0, qty=1)}
    _run_with_dead_ltp(
        positions, _state(), n_ticks=50,
        in_market_hours=True,
    )
    trade = positions["HAL"]
    fail_keys = [k for k in trade.keys() if k.startswith("_ltp_fail_")]
    assert fail_keys == [], (
        f"RECON positions must not accumulate _ltp_fail_* counters; got: {fail_keys}"
    )
    assert not trade.get("_emergency_close_fired"), (
        "RECON positions must never set _emergency_close_fired"
    )


# ============================================================================
# Fix B — emergency_close gated by market hours
# ============================================================================

def test_pre_market_ltp_unavailable_does_not_fire_emergency_close(monkeypatch):
    """The 04:30 IST scenario: non-RECON MIS position with dead LTP.
    Outside market hours, no emergency close should fire (Zerodha rejects
    pre-market orders anyway; spamming them just fills the log).
    """
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"INFY": _normal_trade()}
    rec = _run_with_dead_ltp(
        positions, _state(), n_ticks=20,
        in_market_hours=False,
    )
    assert rec.closes == [], (
        f"emergency_close must not fire outside market hours; got: {rec.closes}"
    )


def test_pre_market_ltp_unavailable_does_not_increment_fail_counter(monkeypatch):
    """Outside market hours the LTP-fail path is a no-op — no counter, no log."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"INFY": _normal_trade()}
    _run_with_dead_ltp(
        positions, _state(), n_ticks=20,
        in_market_hours=False,
    )
    trade = positions["INFY"]
    fail_keys = [k for k in trade.keys() if k.startswith("_ltp_fail_")]
    assert fail_keys == [], (
        "Pre-market LTP failures must not accumulate fail counters"
    )


# ============================================================================
# Fix C — emergency_close fires ONCE per session per symbol
# ============================================================================

def test_market_hours_ltp_fail_fires_emergency_close_once_not_repeatedly(monkeypatch):
    """During market hours with a real MIS position, LTP fails 10 times.
    emergency_close should fire EXACTLY once (after the 3rd fail), not 8 times
    (every cycle past the threshold). The Friday HAL log shows 615 fires;
    bounded to 1.
    """
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"INFY": _normal_trade()}
    rec = _run_with_dead_ltp(
        positions, _state(), n_ticks=10,
        in_market_hours=True,
    )
    emergency = [c for c in rec.closes if c[1] == "LTP_UNAVAILABLE"]
    assert len(emergency) == 1, (
        f"emergency_close must fire exactly once per session per symbol; "
        f"got {len(emergency)} fires: {emergency}"
    )
    # The flag should be set on the trade dict to prevent re-fire.
    assert positions["INFY"].get("_emergency_close_fired") is True


def test_ltp_recovery_rearms_emergency_close_for_future_outage(monkeypatch):
    """If LTP comes back alive, the once-per-session flag clears so the next
    outage in the same session can re-trigger if needed (e.g. broker feed
    flaps twice in one day)."""
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"INFY": _normal_trade()}
    # First outage — 4 ticks, fires once.
    rec1 = _Recorder()
    with patch.object(EE, "_is_market_hours", return_value=True):
        for _ in range(4):
            EE.monitor_positions(
                _state(), positions,
                get_ltp=lambda s: None, close_position=rec1,
                force_exit_check=lambda: False,
            )
    assert len([c for c in rec1.closes if c[1] == "LTP_UNAVAILABLE"]) == 1
    assert positions["INFY"].get("_emergency_close_fired") is True
    # Recovery tick — LTP returns a number; flag must clear.
    with patch.object(EE, "_is_market_hours", return_value=True):
        EE.monitor_positions(
            _state(), positions,
            get_ltp=lambda s: 100.0, close_position=lambda *a, **k: True,
            force_exit_check=lambda: False,
        )
    assert not positions["INFY"].get("_emergency_close_fired"), (
        "Flag must clear on successful LTP fetch to re-arm for future outage"
    )
    fail_keys = [k for k in positions["INFY"].keys() if k.startswith("_ltp_fail_")]
    assert fail_keys == [], "Fail counter must reset on successful LTP fetch"


# ============================================================================
# Friday May 15 regression — combined scenario
# ============================================================================

def test_friday_may15_hal_scenario_produces_zero_closes(monkeypatch):
    """End-to-end: HAL (RECON CNC) + dead LTP feed + pre-market hour.
    All three fix layers must apply. Zero emergency closes expected
    across 50 ticks (the same scenario that produced 615 fires in prod).
    """
    monkeypatch.setattr(CFG, "SKIP_AUTO_EXIT_FOR_RECON", True, raising=False)
    positions = {"HAL": _recon_trade(side="BUY", entry=4681.0, qty=1)}
    rec = _run_with_dead_ltp(
        positions, _state(), n_ticks=50,
        in_market_hours=False,
    )
    assert rec.closes == [], (
        f"Friday HAL scenario must produce ZERO closes; got: {rec.closes}"
    )
    assert not positions["HAL"].get("_emergency_close_fired"), (
        "HAL must not have _emergency_close_fired set under any combination of "
        "RECON tier + pre-market hour + dead LTP"
    )
