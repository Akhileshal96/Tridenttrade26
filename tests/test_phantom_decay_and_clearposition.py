"""Audit fix (2026-05-17): phantom-decay reconciliation + /clearposition.

Bug context:
  The trading_cycle._reconcile_with_broker `if broker_count == 0: skip
  removal to protect local state` guard was added to prevent network
  glitches from wiping the position dict. But it had NO time limit —
  so a position the user sold via Zerodha (outside the bot) stayed
  forever as a phantom in local state, inflating reported P&L by its
  fake unrealized.

  Friday May 15 audit found HAL in this state for an unknown duration.
  All "unrealized=+₹294.80" lines were fictional — the user does not
  actually hold HAL.

Fixes:
  A. PHANTOM_DECAY_TICKS: per-symbol "broker_missing_streak" counter.
     After N consecutive empty broker responses (default 10 ≈ 3 minutes
     in market hours), accept broker reality and remove the phantom.
     Still tolerates 1-2 tick network glitches (the original intent).

  B. clear_phantom_position(sym) + /clearposition Telegram command:
     manual override for the user to wipe a single position from local
     state without placing any broker order. For immediate cleanup.
"""
import json
import os
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import trading_cycle as CYCLE
from state_lock import STATE_LOCK

IST = ZoneInfo("Asia/Kolkata")


def _setup_phantom_position(sym: str):
    """Build a STATE with ONLY the given phantom symbol + live_override on.

    Defensively wipes STATE keys that prior tests may have polluted: positions,
    open_trades (alias), _phantom_missing_streak (decay tracker), and the
    once-per-trip empty-broker log flag. Tests in this file are stateful by
    necessity (they mutate STATE directly), so isolation must be explicit.
    """
    fresh_positions = {
        sym: {"side": "BUY", "qty": 1, "entry": 4681.0,
              "entry_price": 4681.0, "product": "CNC",
              "confidence_tier": "RECON",
              "strategy_family": "reconciled_external"}
    }
    with STATE_LOCK:
        # Wipe-and-replace both dicts so we don't inherit stray symbols from
        # prior tests (test_holdings_reconcile leaves INFY, for example).
        CYCLE.STATE["positions"] = fresh_positions
        CYCLE.STATE["open_trades"] = fresh_positions
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE["_phantom_missing_streak"] = {}
        CYCLE.STATE.pop("_recon_broker_empty_logged", None)
        # CRITICAL: wipe holdings cache too. test_may16_log_quality_fixes
        # leaves an INFY holding in _holdings_cache; the next reconcile
        # would re-hydrate INFY into positions via the holdings path,
        # silently invalidating any phantom-decay assertion on a different
        # symbol.
        CYCLE.STATE.pop("_holdings_cache", None)
        CYCLE.STATE.pop("_holdings_cache_ts", None)


def _patch_empty_broker(monkeypatch, is_live: bool = True):
    """Make get_kite return a kite that has no positions and no holdings.

    is_live: whether the bot is in REAL-order mode. Corrected 2026-05-21:
    phantom-decay now gates on is_live_enabled() (= initiated AND (IS_LIVE
    or live_override)), NOT CFG.IS_LIVE alone. So to simulate "really
    placing live orders" we must set BOTH IS_LIVE=true AND initiated=true.
    To simulate paper, we set initiated=false (orders simulate even if
    IS_LIVE happens to be true — which was exactly the Day 4 bug state).
    """
    monkeypatch.setattr(CFG, "IS_LIVE", is_live, raising=False)
    with STATE_LOCK:
        CYCLE.STATE["initiated"] = bool(is_live)
        CYCLE.STATE["live_override"] = False

    class _EmptyKite:
        def positions(self): return {"net": []}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _EmptyKite(), raising=False)
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)


# ============================================================================
# Fix A — phantom-decay
# ============================================================================

def test_phantom_decays_after_threshold_ticks(monkeypatch):
    """The Friday HAL scenario: phantom in local, broker empty forever.
    After PHANTOM_DECAY_TICKS ticks the phantom is removed."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("HAL")
    _patch_empty_broker(monkeypatch)

    # 4 ticks — still under threshold, phantom survives
    for i in range(4):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass
    assert "HAL" in CYCLE.STATE["positions"], (
        "phantom must survive while under decay threshold"
    )
    assert CYCLE.STATE["_phantom_missing_streak"]["HAL"] == 4

    # 5th tick — threshold met, phantom removed
    try:
        CYCLE.reconcile_broker_positions()
    except Exception:
        pass
    assert "HAL" not in CYCLE.STATE["positions"], (
        f"phantom must be removed at threshold; state: {CYCLE.STATE['positions']}"
    )
    # Tracker entry for the decayed symbol should be cleaned up.
    assert "HAL" not in (CYCLE.STATE.get("_phantom_missing_streak") or {})


def test_phantom_streak_resets_when_broker_recovers(monkeypatch):
    """If broker starts returning positions again mid-streak, the
    decay counter resets so the position isn't unfairly culled."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("HAL")
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)

    # 3 ticks empty → streak=3
    class _EmptyKite:
        def positions(self): return {"net": []}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _EmptyKite(), raising=False)
    for _ in range(3):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass
    assert CYCLE.STATE["_phantom_missing_streak"]["HAL"] == 3

    # Broker recovers: returns HAL. Streak must reset.
    class _GoodKite:
        def positions(self):
            return {"net": [{"tradingsymbol": "HAL", "quantity": 1,
                             "average_price": 4681.0, "product": "CNC"}]}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _GoodKite(), raising=False)
    try:
        CYCLE.reconcile_broker_positions()
    except Exception:
        pass
    assert CYCLE.STATE.get("_phantom_missing_streak") == {}, (
        "streak tracker must be wiped on broker recovery"
    )
    assert "HAL" in CYCLE.STATE["positions"], (
        "HAL must still be in positions (broker confirmed it)"
    )


def test_phantom_only_decays_after_full_threshold(monkeypatch):
    """Sanity: with default threshold (10), 9 empty ticks doesn't decay."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 10, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 10 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("HAL")
    _patch_empty_broker(monkeypatch)

    for _ in range(9):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass
    assert "HAL" in CYCLE.STATE["positions"], (
        "phantom must NOT decay before threshold (10 ticks)"
    )


def test_phantom_decay_handles_multiple_symbols_independently(monkeypatch):
    """If two phantoms have different ages, only the one past threshold decays."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    fresh_positions = {
        "HAL": {"side": "BUY", "qty": 1, "entry": 4681.0, "product": "CNC"},
        "INFY": {"side": "BUY", "qty": 10, "entry": 1500.0, "product": "MIS"},
    }
    with STATE_LOCK:
        CYCLE.STATE["positions"] = fresh_positions
        CYCLE.STATE["open_trades"] = fresh_positions
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE["_phantom_missing_streak"] = {"HAL": 4}  # one short of threshold
        CYCLE.STATE.pop("_recon_broker_empty_logged", None)
        CYCLE.STATE.pop("_holdings_cache", None)
        CYCLE.STATE.pop("_holdings_cache_ts", None)
    _patch_empty_broker(monkeypatch)

    try:
        CYCLE.reconcile_broker_positions()
    except Exception:
        pass

    # HAL was at 4, +1 = 5 ≥ threshold → decayed.
    # INFY just started at 1 → still in.
    assert "HAL" not in CYCLE.STATE["positions"]
    assert "INFY" in CYCLE.STATE["positions"]
    assert CYCLE.STATE["_phantom_missing_streak"].get("INFY") == 1


# ============================================================================
# Fix B — clear_phantom_position + /clearposition
# ============================================================================

def test_clear_phantom_position_removes_from_state(monkeypatch):
    """The manual escape hatch: user can wipe a stuck position immediately
    without waiting PHANTOM_DECAY_TICKS ticks."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)
    _setup_phantom_position("HAL")

    msg = CYCLE.clear_phantom_position("HAL")
    assert "HAL" not in CYCLE.STATE["positions"]
    assert "removed from local state" in msg
    assert "side=BUY" in msg


def test_clear_phantom_position_no_op_for_missing_symbol(monkeypatch):
    """Symbol not in state → friendly message, no state mutation."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}

    msg = CYCLE.clear_phantom_position("NONEXISTENT")
    assert "not in local positions" in msg


def test_clear_phantom_position_clears_decay_tracker(monkeypatch):
    """If a phantom is mid-decay (counter > 0) and user clears manually,
    the counter must be cleared too — otherwise the next recon misreads."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)
    _setup_phantom_position("HAL")
    CYCLE.STATE["_phantom_missing_streak"] = {"HAL": 7}

    CYCLE.clear_phantom_position("HAL")
    assert "HAL" not in (CYCLE.STATE.get("_phantom_missing_streak") or {})


def test_clear_phantom_position_does_not_call_broker(monkeypatch):
    """Critical: this is local-state cleanup, NOT a sell order. Must not
    invoke any broker function — that's what differentiates it from /panic."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)
    _setup_phantom_position("HAL")

    kite_called = {"n": 0}
    def boom():
        kite_called["n"] += 1
        raise AssertionError("get_kite must NOT be called from clear_phantom_position")
    monkeypatch.setattr(CYCLE, "get_kite", boom, raising=False)

    CYCLE.clear_phantom_position("HAL")
    assert kite_called["n"] == 0


# ============================================================================
# Fix C — phantom-decay must NOT fire in paper mode
# (audit fix 2026-05-19: Day 2 KOTAKBANK paper trade killed at 12:07)
# ============================================================================

def test_phantom_decay_does_NOT_fire_in_paper_mode(monkeypatch):
    """The Day 2 (May 19) bug: in paper mode, the broker NEVER sees paper
    positions, so broker_count==0 is the natural state — NOT evidence the
    user sold elsewhere. Phantom-decay treating this as "phantom" wrongly
    killed a legitimate paper trade (KOTAKBANK 11:51 → 12:07 RECON_PHANTOM_DECAY).

    With IS_LIVE=false the decay path must short-circuit even when
    `live_override=true` (e.g., user /arm'd while in paper to see /holdings)
    AND broker_count==0 AND many cycles have accumulated.
    """
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("KOTAKBANK")
    # IS_LIVE=False: that's the entire point of this test.
    _patch_empty_broker(monkeypatch, is_live=False)

    # Run 20 cycles — 4x the decay threshold. In LIVE mode this would
    # have wiped the position 4 times over. In PAPER mode it must survive.
    for _ in range(20):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass

    assert "KOTAKBANK" in CYCLE.STATE["positions"], (
        f"paper position must NOT be phantom-decayed; "
        f"state: {list(CYCLE.STATE['positions'].keys())}"
    )


def test_phantom_decay_skipped_when_live_config_but_not_initiated(monkeypatch):
    """The Day 4 (May 21) bug: IS_LIVE=true in .env but the bot is NOT
    initiated (not armed). Orders simulate ([paper], order_id=-) but the
    2026-05-19 fix gated phantom-decay on CFG.IS_LIVE (true) so it still
    fired — killing KOTAKBANK @12:42 and ITC @13:57.

    Corrected gate is is_live_enabled() = initiated AND (IS_LIVE or
    live_override). With initiated=false, phantom-decay must skip even
    though CFG.IS_LIVE is true and reconcile itself runs.
    """
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("KOTAKBANK")
    # The exact Day-4 state: IS_LIVE=true, but NOT initiated → orders simulate.
    monkeypatch.setattr(CFG, "IS_LIVE", True, raising=False)
    with STATE_LOCK:
        CYCLE.STATE["initiated"] = False        # not armed → paper orders
        CYCLE.STATE["live_override"] = False
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)

    class _EmptyKite:
        def positions(self): return {"net": []}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _EmptyKite(), raising=False)

    # reconcile RUNS (IS_LIVE=true) but phantom-decay must NOT fire
    # (is_live_enabled() is false because initiated=false).
    for _ in range(20):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass

    assert "KOTAKBANK" in CYCLE.STATE["positions"], (
        f"simulated/paper position (IS_LIVE=true but not initiated) must NOT "
        f"be phantom-decayed; state: {list(CYCLE.STATE['positions'].keys())}"
    )


def test_phantom_decay_still_fires_in_live_mode(monkeypatch):
    """Sanity / regression: with IS_LIVE=true, the original decay behavior
    must still work — paper-mode skip must not have neutered the fix for
    real live-mode phantoms (which is what shipped on 2026-05-17)."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("HAL")
    # IS_LIVE=True (the live-mode case the original fix was designed for).
    _patch_empty_broker(monkeypatch, is_live=True)

    for _ in range(6):  # 1 over threshold
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass

    assert "HAL" not in CYCLE.STATE["positions"], (
        "live-mode phantom must still be decayed by the original 2026-05-17 fix"
    )


def test_paper_mode_does_not_accumulate_missing_streak(monkeypatch):
    """In paper mode, the per-symbol missing_streak counter should not
    accumulate at all — even reading it is wasted work, but more importantly,
    if IS_LIVE flips to true later (user runs /arm during live hours), the
    counter shouldn't be pre-loaded with stale paper-mode ticks that would
    immediately trip the threshold."""
    monkeypatch.setattr(CFG, "PHANTOM_DECAY_TICKS", 5, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda k, d=None: 5 if k == "PHANTOM_DECAY_TICKS" else
                                          (True if k == "USE_HOLDINGS_RECONCILE" else d),
                        raising=False)
    _setup_phantom_position("KOTAKBANK")
    _patch_empty_broker(monkeypatch, is_live=False)

    for _ in range(10):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass

    # Either: tracker never created, OR tracker exists but KOTAKBANK never added.
    streak = CYCLE.STATE.get("_phantom_missing_streak") or {}
    assert streak.get("KOTAKBANK", 0) == 0, (
        f"paper-mode reconciles must not accumulate decay counters; "
        f"streak: {streak}"
    )
