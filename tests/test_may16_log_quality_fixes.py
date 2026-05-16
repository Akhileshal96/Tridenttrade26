"""Audit fixes (2026-05-16): log-quality + robustness fixes from Friday May 15.

Covers four companion fixes shipped alongside the LTP-fail spam fix
(see test_ltp_fail_spam_fix.py):

  #4  trading_cycle._place_live_order — fast-fail on auth errors instead
      of retrying 3× per call. Friday's HAL loop emitted 1,845 auth ERROR
      lines (3 retries × 615 emergency-close cycles). Bounded to 1 ERROR
      per attempt.

  #5  trading_cycle._reconcile_with_broker — idempotent "broker returned
      0 positions but local has N" log. Friday→Saturday emitted ~2,000
      identical WARN lines (one per 20s tick); bounded to one per
      state-transition (clears on next non-empty broker response).

  #3  trading_cycle._reconcile_with_broker — don't poison the holdings
      cache with empty fetches. kite.holdings() can transiently return []
      on weekends/holidays/right-after-close; caching that result hid
      legitimate holdings (e.g. HAL) for 5 min after every empty fetch.

  #2  trading_cycle._load_state_snapshot — normalize the `side` field
      on restore. The Friday "Placing BUY 1x HAL" mystery implied HAL
      was stored with side="SHORT" for a position that is actually LONG.
      RECON CNC positions (holdings) cannot be SHORT — force to BUY.
"""
import os
import sys
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import trading_cycle as CYCLE
from state_lock import STATE_LOCK

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Fix #4 — _place_live_order: fast-fail on auth errors
# ============================================================================

def test_place_live_order_fails_fast_on_access_token_error(monkeypatch):
    """1,845 BUY HAL attempts in Friday's log = 3 retries × 615 cycles.
    Auth errors don't recover in 0.6s — only the TOTP scheduler can.
    Bail at retry 0 → 1 ERROR per call, not 3."""
    cap = []
    monkeypatch.setattr(CYCLE, "append_log",
                        lambda lvl, tag, msg: cap.append((lvl, tag, msg)),
                        raising=False)
    monkeypatch.setattr(CYCLE, "evaluate_ip_compliance",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_validated_market_protection",
                        lambda *a, **k: 0.2, raising=False)
    monkeypatch.setattr(CYCLE, "_order_rate_limit_wait",
                        lambda: None, raising=False)
    # Force live_order_allowed True so the function reaches the try/except.
    with STATE_LOCK:
        CYCLE.STATE["live_order_allowed"] = True

    call_count = {"n": 0}
    def boom(*a, **k):
        call_count["n"] += 1
        raise Exception("Incorrect `api_key` or `access_token`.")
    monkeypatch.setattr(CYCLE, "place_order_safe", boom, raising=False)

    class _FakeKite:
        ORDER_TYPE_MARKET = "MARKET"
        ORDER_TYPE_SLM = "SL-M"
        VARIETY_REGULAR = "regular"
        TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"

    result = CYCLE._place_live_order(_FakeKite(), "HAL", "BUY", 1,
                                     product_override="CNC")
    assert result is None, "auth-failed order must return None"
    assert call_count["n"] == 1, (
        f"place_order_safe should be called ONCE on auth error (no retry); "
        f"got {call_count['n']}"
    )
    auth_logs = [r for r in cap if "auth_failure" in r[2]]
    assert len(auth_logs) == 1, (
        f"exactly one auth_failure ERROR expected; got {len(auth_logs)}: {auth_logs}"
    )


def test_place_live_order_fails_fast_on_api_key_error(monkeypatch):
    """Same as above but for the 'Incorrect api_key' wording (Zerodha
    sometimes returns this variant)."""
    cap = []
    monkeypatch.setattr(CYCLE, "append_log",
                        lambda lvl, tag, msg: cap.append((lvl, tag, msg)),
                        raising=False)
    monkeypatch.setattr(CYCLE, "evaluate_ip_compliance",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_validated_market_protection",
                        lambda *a, **k: 0.2, raising=False)
    monkeypatch.setattr(CYCLE, "_order_rate_limit_wait",
                        lambda: None, raising=False)
    with STATE_LOCK:
        CYCLE.STATE["live_order_allowed"] = True

    call_count = {"n": 0}
    def boom(*a, **k):
        call_count["n"] += 1
        raise Exception("Incorrect `api_key` for app")
    monkeypatch.setattr(CYCLE, "place_order_safe", boom, raising=False)

    class _FakeKite:
        ORDER_TYPE_MARKET = "MARKET"; ORDER_TYPE_SLM = "SL-M"
        VARIETY_REGULAR = "regular"; TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"

    CYCLE._place_live_order(_FakeKite(), "HAL", "BUY", 1, product_override="CNC")
    assert call_count["n"] == 1


def test_place_live_order_still_retries_on_429_rate_limit(monkeypatch):
    """Sanity: the auth-fast-fail must not regress the existing 429 retry path."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "evaluate_ip_compliance",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_validated_market_protection",
                        lambda *a, **k: 0.2, raising=False)
    monkeypatch.setattr(CYCLE, "_order_rate_limit_wait",
                        lambda: None, raising=False)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_: None, raising=False)
    with STATE_LOCK:
        CYCLE.STATE["live_order_allowed"] = True

    call_count = {"n": 0}
    def rate_limited(*a, **k):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise Exception("429 Too Many Requests")
        return "order_id_xyz"
    monkeypatch.setattr(CYCLE, "place_order_safe", rate_limited, raising=False)

    class _FakeKite:
        ORDER_TYPE_MARKET = "MARKET"; ORDER_TYPE_SLM = "SL-M"
        VARIETY_REGULAR = "regular"; TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"

    result = CYCLE._place_live_order(_FakeKite(), "INFY", "BUY", 10,
                                     product_override="MIS")
    assert result == "order_id_xyz"
    assert call_count["n"] == 3, "429 should still retry up to 3 times"


# ============================================================================
# Fix #5 — Idempotent "broker returned 0 positions" log
# ============================================================================

def test_recon_broker_empty_log_emits_once_per_trip(monkeypatch):
    """Friday→Saturday produced ~2,000 identical WARN lines. Idempotency
    bounds it to one log per state transition."""
    cap = []
    monkeypatch.setattr(CYCLE, "append_log",
                        lambda lvl, tag, msg: cap.append((lvl, tag, msg)),
                        raising=False)

    # Build a STATE with a local position and clear the once-flag.
    # live_override=True bypasses the IS_LIVE guard in reconcile_broker_positions.
    with STATE_LOCK:
        CYCLE.STATE["positions"] = {"HAL": {"side": "BUY", "qty": 1, "entry": 4681.0}}
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE.pop("_recon_broker_empty_logged", None)

    # Fake kite returning empty positions + empty holdings = broker_count 0.
    class _EmptyKite:
        def positions(self): return {"net": []}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _EmptyKite(), raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda key, default=None: default, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event",
                        lambda *a, **k: None, raising=False)

    # Run reconcile 10× — should produce ONE WARN, not 10.
    for _ in range(10):
        try:
            CYCLE.reconcile_broker_positions()
        except Exception:
            pass  # Best-effort; we only care about log counts.

    warns = [r for r in cap if "broker returned 0 positions" in r[2]]
    assert len(warns) == 1, (
        f"broker-empty WARN must emit ONCE per trip; got {len(warns)} times"
    )
    assert CYCLE.STATE.get("_recon_broker_empty_logged") is True


def test_recon_broker_empty_flag_clears_on_recovery(monkeypatch):
    """When broker starts returning positions again, the once-flag clears
    so a subsequent empty-broker event re-warns."""
    cap = []
    monkeypatch.setattr(CYCLE, "append_log",
                        lambda lvl, tag, msg: cap.append((lvl, tag, msg)),
                        raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda key, default=None: default, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event",
                        lambda *a, **k: None, raising=False)
    # Pre-set the flag — simulates "we already warned"
    with STATE_LOCK:
        CYCLE.STATE["positions"] = {"HAL": {"side": "BUY", "qty": 1, "entry": 4681.0, "product": "CNC"}}
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE["_recon_broker_empty_logged"] = True

    # Now broker returns HAL again (recovery).
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
    assert CYCLE.STATE.get("_recon_broker_empty_logged") is False, (
        "flag must clear when broker reports positions again"
    )


# ============================================================================
# Fix #3 — Holdings cache: don't cache empty fetches
# ============================================================================

def test_holdings_cache_skipped_when_kite_returns_empty(monkeypatch):
    """The Saturday scenario: kite.holdings() returns [] (weekend) but HAL
    is genuinely held. Old behavior: cache [] for 5 min, hiding HAL.
    New behavior: don't pollute cache with empties; keep the prior good
    cache instead."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)
    # Allow USE_HOLDINGS_RECONCILE to be on (the default).
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda key, default=None: True if key == "USE_HOLDINGS_RECONCILE" else default,
                        raising=False)
    # Seed cache with the genuine prior HAL holding.
    prior_holding = [{"tradingsymbol": "HAL", "quantity": 1, "t1_quantity": 0,
                      "average_price": 4681.0}]
    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}  # start flat
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE["_holdings_cache"] = list(prior_holding)
        CYCLE.STATE["_holdings_cache_ts"] = 0.0  # forces stale → refetch

    # New fetch returns empty (weekend mode).
    class _WeekendKite:
        def positions(self): return {"net": []}
        def holdings(self): return []
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _WeekendKite(), raising=False)

    try:
        CYCLE.reconcile_broker_positions()
    except Exception:
        pass

    # The cache must STILL hold the prior non-empty value.
    cached = CYCLE.STATE.get("_holdings_cache")
    assert cached == prior_holding, (
        f"empty kite.holdings() must not overwrite a good cache; cache is now {cached}"
    )


def test_holdings_cache_updated_when_kite_returns_non_empty(monkeypatch):
    """Sanity: a real non-empty fetch DOES update the cache."""
    monkeypatch.setattr(CYCLE, "append_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_log_trade_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(CYCLE, "_cfg_get",
                        lambda key, default=None: True if key == "USE_HOLDINGS_RECONCILE" else default,
                        raising=False)
    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}
        CYCLE.STATE["live_override"] = True
        CYCLE.STATE.pop("_holdings_cache", None)
        CYCLE.STATE.pop("_holdings_cache_ts", None)

    fresh_holding = [{"tradingsymbol": "INFY", "quantity": 5, "t1_quantity": 0,
                      "average_price": 1500.0}]
    class _LiveKite:
        def positions(self): return {"net": []}
        def holdings(self): return fresh_holding
    monkeypatch.setattr(CYCLE, "get_kite", lambda: _LiveKite(), raising=False)

    try:
        CYCLE.reconcile_broker_positions()
    except Exception:
        pass

    cached = CYCLE.STATE.get("_holdings_cache")
    assert cached == fresh_holding, "non-empty kite.holdings() must update cache"


# ============================================================================
# Fix #2 — side normalization in _load_state_snapshot
# ============================================================================

def test_snapshot_restore_normalizes_recon_cnc_short_to_buy(tmp_path, monkeypatch):
    """Friday's HAL "Placing BUY" mystery: RECON CNC position somehow stored
    with side="SHORT". Holdings can't be SHORT — normalize to BUY on restore."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    snap = {
        "day_key": today,
        "today_pnl": 0.0,
        "positions": {
            "HAL": {
                "side": "SHORT",  # ← the bug: stale storage
                "qty": 1, "quantity": 1,
                "entry": 4681.0, "entry_price": 4681.0,
                "confidence_tier": "RECON",
                "strategy_family": "reconciled_external",
                "product": "CNC",
            }
        },
    }
    snap_path = tmp_path / "state_snapshot.json"
    snap_path.write_text(json.dumps(snap))
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_path), raising=False)

    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}

    CYCLE._load_state_snapshot()
    hal = CYCLE.STATE["positions"].get("HAL")
    assert hal is not None, "HAL must restore from snapshot"
    assert hal["side"] == "BUY", (
        f"RECON CNC holdings can't be SHORT — must normalize to BUY; got {hal['side']}"
    )


def test_snapshot_restore_preserves_legitimate_short_on_mis(tmp_path, monkeypatch):
    """A real intraday SHORT (MIS) must NOT be flipped to BUY — only RECON
    CNC positions get the normalization."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    snap = {
        "day_key": today,
        "today_pnl": 0.0,
        "positions": {
            "NIFTY_PUT": {
                "side": "SHORT",
                "qty": 50, "quantity": 50,
                "entry": 200.0, "entry_price": 200.0,
                "confidence_tier": "FULL",
                "strategy_family": "short_breakdown",
                "product": "MIS",
            }
        },
    }
    snap_path = tmp_path / "state_snapshot.json"
    snap_path.write_text(json.dumps(snap))
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_path), raising=False)

    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}

    CYCLE._load_state_snapshot()
    pos = CYCLE.STATE["positions"].get("NIFTY_PUT")
    assert pos is not None
    assert pos["side"] == "SHORT", (
        "Legitimate MIS short must not be normalized to BUY"
    )


def test_snapshot_restore_canonicalizes_long_aliases_to_buy(tmp_path, monkeypatch):
    """side="LONG" / "B" should normalize to "BUY"."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    snap = {
        "day_key": today,
        "today_pnl": 0.0,
        "positions": {
            "INFY": {
                "side": "LONG",  # alias for BUY
                "qty": 10, "quantity": 10,
                "entry": 1500.0, "entry_price": 1500.0,
                "confidence_tier": "FULL",
                "strategy_family": "trend_long",
                "product": "MIS",
            }
        },
    }
    snap_path = tmp_path / "state_snapshot.json"
    snap_path.write_text(json.dumps(snap))
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_path), raising=False)

    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}

    CYCLE._load_state_snapshot()
    pos = CYCLE.STATE["positions"].get("INFY")
    assert pos["side"] == "BUY", (
        f'side="LONG" must canonicalize to "BUY"; got {pos["side"]}'
    )


def test_snapshot_restore_clears_emergency_close_flag(tmp_path, monkeypatch):
    """A restart re-arms the once-per-session emergency-close flag so the
    bot doesn't carry a stale "already fired" into the new session."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    snap = {
        "day_key": today,
        "today_pnl": 0.0,
        "positions": {
            "INFY": {
                "side": "BUY", "qty": 10, "quantity": 10,
                "entry": 1500.0, "entry_price": 1500.0,
                "confidence_tier": "FULL",
                "strategy_family": "trend_long",
                "product": "MIS",
                "_emergency_close_fired": True,  # ← stale
                "_ltp_fail_INFY": 25,            # ← stale
            }
        },
    }
    snap_path = tmp_path / "state_snapshot.json"
    snap_path.write_text(json.dumps(snap))
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_path), raising=False)

    with STATE_LOCK:
        CYCLE.STATE["positions"] = {}

    CYCLE._load_state_snapshot()
    pos = CYCLE.STATE["positions"].get("INFY")
    assert "_emergency_close_fired" not in pos, (
        "_emergency_close_fired must be stripped on restore"
    )
    assert "_ltp_fail_INFY" not in pos, (
        "_ltp_fail_* must be stripped on restore"
    )
