"""Tests for the 2026-05-08 holdings reconciliation fix.

Bug context:
  reconcile_broker_positions() only fetched kite.positions(), which omits
  multi-day CNC holdings. Any HYBRID-to-CNC swing position held overnight
  appeared "missing" on day N+1 and got marked RECON_BROKER_FLAT, producing
  phantom reconciled_external entries (e.g. HAL on 2026-05-08 with entry
  4794, exit 4740.70, P&L +₹53.30 — wrong because the real entry was
  ₹4681 from 2026-05-07).

Fix: also fetch kite.holdings() and merge into broker_map. Holdings are
always CNC LONG; quantity + t1_quantity = total owned shares.
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())

import config as CFG
import trading_cycle as CYCLE


# ============================================================================
# Helpers
# ============================================================================

class _FakeKite:
    """Stub kite client for reconciliation testing."""
    def __init__(self, positions_resp, holdings_resp):
        self._positions = positions_resp
        self._holdings = holdings_resp
        self.positions_calls = 0
        self.holdings_calls = 0

    def positions(self):
        self.positions_calls += 1
        return self._positions

    def holdings(self):
        self.holdings_calls += 1
        return self._holdings


def _patch_runtime(monkeypatch, kite, live=True):
    monkeypatch.setattr(CFG, "IS_LIVE", live, raising=False)
    monkeypatch.setattr(CYCLE, "get_kite", lambda: kite)
    # Avoid touching the real PM/log_store
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None)


def _reset_state():
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["positions"] = {}
        CYCLE.STATE["_holdings_cache"] = None
        CYCLE.STATE["_holdings_cache_ts"] = 0.0
        CYCLE.STATE["live_override"] = False


# ============================================================================
# Holdings merge
# ============================================================================

def test_holdings_recovered_when_positions_empty(monkeypatch):
    """Symbol present only in holdings (overnight CNC) is recovered into
    broker_map and the local position is preserved (not marked RECON_BROKER_FLAT)."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)
    monkeypatch.setattr(CFG, "HOLDINGS_CACHE_TTL_SEC", 300, raising=False)

    # Local has HAL CNC from yesterday
    CYCLE.STATE["positions"] = {
        "HAL": {
            "symbol": "HAL", "side": "BUY", "entry": 4681.0, "qty": 1,
            "product": "CNC", "trade_mode": "SWING",
            "strategy_family": "trend_long",
        }
    }

    # Broker positions are empty; holdings has HAL
    kite = _FakeKite(
        positions_resp={"net": []},
        holdings_resp=[
            {"tradingsymbol": "HAL", "quantity": 1, "t1_quantity": 0,
             "average_price": 4681.0, "product": "CNC"}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    # Critical: HAL must still be in local positions (not removed)
    assert "HAL" in CYCLE.STATE["positions"], (
        "HAL CNC swing was wiped — holdings reconcile failed"
    )
    # And broker queries fired
    assert kite.positions_calls == 1
    assert kite.holdings_calls == 1


def test_holdings_disabled_via_flag_falls_back_to_old_behavior(monkeypatch):
    """When USE_HOLDINGS_RECONCILE=False, the old (buggy) behavior runs.
    This test confirms the toggle works for emergency disable."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", False, raising=False)

    CYCLE.STATE["positions"] = {
        "HAL": {"symbol": "HAL", "side": "BUY", "entry": 4681.0, "qty": 1, "product": "CNC"}
    }

    kite = _FakeKite(
        positions_resp={"net": []},
        # holdings_resp HAS HAL but flag is off → ignored
        holdings_resp=[{"tradingsymbol": "HAL", "quantity": 1, "average_price": 4681.0}],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    # Holdings call should NOT have fired
    assert kite.holdings_calls == 0
    # Old behavior: broker_count=0, local_count=1 → skip removal (safety net still
    # kicks in from the existing "broker returned 0 positions" guard)
    assert "HAL" in CYCLE.STATE["positions"]


def test_holdings_t1_quantity_counted(monkeypatch):
    """A holding with quantity=0 but t1_quantity=2 (just bought, not yet
    settled) must still count as owned."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)

    # Local has nothing; broker has nothing in positions but T1 holding in INFY
    kite = _FakeKite(
        positions_resp={"net": []},
        holdings_resp=[
            {"tradingsymbol": "INFY", "quantity": 0, "t1_quantity": 2,
             "average_price": 1180.0, "product": "CNC"}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    # INFY should have been reconciled in as a CNC LONG of qty 2
    assert "INFY" in CYCLE.STATE["positions"]
    infy = CYCLE.STATE["positions"]["INFY"]
    assert infy.get("qty") == 2 or infy.get("quantity") == 2
    assert str(infy.get("side") or "").upper() == "BUY"


def test_holdings_with_zero_qty_skipped(monkeypatch):
    """Holdings entry with quantity=0 AND t1_quantity=0 (e.g., fully sold
    but still in the response) must NOT be reconciled."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)

    kite = _FakeKite(
        positions_resp={"net": []},
        holdings_resp=[
            {"tradingsymbol": "ABC", "quantity": 0, "t1_quantity": 0,
             "realised_quantity": 5, "average_price": 100.0}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    assert "ABC" not in CYCLE.STATE["positions"]


def test_holdings_cache_respects_ttl(monkeypatch):
    """Within the TTL window, holdings should be served from cache, not re-fetched."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)
    monkeypatch.setattr(CFG, "HOLDINGS_CACHE_TTL_SEC", 300, raising=False)

    kite = _FakeKite(
        positions_resp={"net": []},
        holdings_resp=[
            {"tradingsymbol": "HAL", "quantity": 1, "t1_quantity": 0,
             "average_price": 4681.0}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    # First call — cache miss
    CYCLE.reconcile_broker_positions()
    assert kite.holdings_calls == 1

    # Second call within TTL — cache hit, no new API call
    CYCLE.reconcile_broker_positions()
    assert kite.holdings_calls == 1, "second reconcile within TTL must use cache"


def test_holdings_cache_refreshes_after_ttl(monkeypatch):
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)
    monkeypatch.setattr(CFG, "HOLDINGS_CACHE_TTL_SEC", 1, raising=False)  # 1 sec TTL

    kite = _FakeKite(
        positions_resp={"net": []},
        holdings_resp=[
            {"tradingsymbol": "HAL", "quantity": 1, "average_price": 4681.0}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()
    assert kite.holdings_calls == 1
    # Force cache expiry
    CYCLE.STATE["_holdings_cache_ts"] = time.time() - 10
    CYCLE.reconcile_broker_positions()
    assert kite.holdings_calls == 2


def test_holdings_fetch_failure_falls_back_to_cache(monkeypatch):
    """If kite.holdings() raises, use last cached holdings instead of failing."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)

    # Pre-seed the cache as if a previous fetch succeeded
    CYCLE.STATE["_holdings_cache"] = [
        {"tradingsymbol": "HAL", "quantity": 1, "average_price": 4681.0}
    ]
    CYCLE.STATE["_holdings_cache_ts"] = 0.0  # expired → triggers refresh attempt

    class _FlakyKite:
        def __init__(self):
            self.positions_calls = 0
            self.holdings_calls = 0
        def positions(self):
            self.positions_calls += 1
            return {"net": []}
        def holdings(self):
            self.holdings_calls += 1
            raise Exception("rate limited")

    kite = _FlakyKite()
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    # Even though the live fetch failed, the bot should have used the cache
    # to keep HAL recognized as a holding.
    assert "HAL" in CYCLE.STATE["positions"]


def test_position_and_holding_combine_when_same_symbol(monkeypatch):
    """If a symbol is in BOTH today's positions (added 1 today) AND yesterday's
    holdings (had 1 from before), total qty should sum to 2."""
    _reset_state()
    monkeypatch.setattr(CFG, "USE_HOLDINGS_RECONCILE", True, raising=False)

    kite = _FakeKite(
        positions_resp={"net": [
            {"tradingsymbol": "HAL", "quantity": 1, "average_price": 4690.0,
             "product": "CNC"}
        ]},
        holdings_resp=[
            {"tradingsymbol": "HAL", "quantity": 1, "t1_quantity": 0,
             "average_price": 4681.0}
        ],
    )
    _patch_runtime(monkeypatch, kite)

    CYCLE.reconcile_broker_positions()

    assert "HAL" in CYCLE.STATE["positions"]
    hal = CYCLE.STATE["positions"]["HAL"]
    # qty 2 = 1 (today's position) + 1 (yesterday's holding)
    assert hal.get("qty") == 2 or hal.get("quantity") == 2


def test_holdings_skipped_when_not_live_mode(monkeypatch):
    """If IS_LIVE=False and live_override=False, no broker calls should fire."""
    _reset_state()
    monkeypatch.setattr(CFG, "IS_LIVE", False, raising=False)
    CYCLE.STATE["live_override"] = False

    kite = _FakeKite(positions_resp={"net": []}, holdings_resp=[])
    monkeypatch.setattr(CYCLE, "get_kite", lambda: kite)

    CYCLE.reconcile_broker_positions()

    assert kite.positions_calls == 0
    assert kite.holdings_calls == 0
