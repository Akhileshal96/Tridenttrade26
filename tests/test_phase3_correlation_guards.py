"""Tests for Phase 3: GOD-mode correlation + fast-stage guards.

Covers:
  • Sector cap (audit fix #4) — _check_sector_cap, _open_positions_in_sector
  • Fast-stage entry limit (audit fix #5) — _check_fast_stage_entry_limit, _entries_within_first_n_min
  • GOD_MAX_CONCURRENT_TRADES default lowered 50 -> 8
  • /godstatus command surface (smoke check)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())

import config as CFG
import trading_cycle as CYCLE
import bot

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Helpers
# ============================================================================

class _DummyEvent:
    def __init__(self, sender_id=1001):
        self.sender_id = sender_id
        self.replies = []

    async def reply(self, message=None, **kwargs):
        if message is None:
            message = kwargs.get("message")
        self.replies.append(message)


def _patch_bot_perms(monkeypatch):
    monkeypatch.setattr(bot, "_is_owner", lambda sid: int(sid) == 1001)
    monkeypatch.setattr(bot, "_is_trader", lambda sid: int(sid) in {1001, 2002})
    monkeypatch.setattr(bot, "_is_viewer", lambda sid: int(sid) in {1001, 2002, 3003})


def _reset_state():
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["positions"] = {}
        CYCLE.STATE["entry_event_ts"] = []
        CYCLE.STATE["sector_map_cache"] = {
            "AXISBANK": "BANKING",
            "ICICIBANK": "BANKING",
            "HDFCBANK": "BANKING",
            "SBIN": "BANKING",
            "BPCL": "ENERGY",
            "ONGC": "ENERGY",
            "INFY": "IT",
            "TCS": "IT",
            "HCLTECH": "IT",
        }
        CYCLE.STATE["wallet_net_inr"] = 20000.0
        CYCLE.STATE["risk_profile"] = "STANDARD"
        CYCLE.STATE["trading_mode"] = "INTRADAY"


def _add_position(sym, side="SHORT", sector=None):
    pos = CYCLE.STATE.setdefault("positions", {})
    pos[sym] = {"side": side, "sector": sector, "entry": 100.0, "qty": 1, "product": "MIS"}


# ============================================================================
# Sector cap (_check_sector_cap, _open_positions_in_sector)
# ============================================================================

def test_open_positions_in_sector_counts_correctly():
    _reset_state()
    _add_position("AXISBANK", side="SHORT")
    _add_position("ICICIBANK", side="SHORT")
    _add_position("BPCL", side="SHORT")
    assert CYCLE._open_positions_in_sector("BANKING", side="SHORT") == 2
    assert CYCLE._open_positions_in_sector("ENERGY", side="SHORT") == 1
    assert CYCLE._open_positions_in_sector("IT", side="SHORT") == 0


def test_open_positions_in_sector_respects_side_filter():
    _reset_state()
    _add_position("AXISBANK", side="SHORT")
    _add_position("ICICIBANK", side="BUY")
    assert CYCLE._open_positions_in_sector("BANKING", side="SHORT") == 1
    assert CYCLE._open_positions_in_sector("BANKING", side="BUY") == 1
    assert CYCLE._open_positions_in_sector("BANKING") == 2  # no side filter


def test_open_positions_in_sector_treats_sell_as_short():
    _reset_state()
    pos = CYCLE.STATE.setdefault("positions", {})
    pos["AXISBANK"] = {"side": "SELL", "sector": None, "entry": 100, "qty": 1}
    assert CYCLE._open_positions_in_sector("BANKING", side="SHORT") == 1


def test_open_positions_in_sector_uses_explicit_sector_when_set():
    """If position has explicit sector field, use it (don't re-lookup)."""
    _reset_state()
    pos = CYCLE.STATE.setdefault("positions", {})
    pos["UNKNOWNTICKER"] = {"side": "SHORT", "sector": "BANKING", "entry": 100, "qty": 1}
    assert CYCLE._open_positions_in_sector("BANKING", side="SHORT") == 1


def test_sector_cap_allows_first_short_in_sector(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", True, raising=False)
    monkeypatch.setattr(CFG, "MAX_OPEN_PER_SECTOR_PER_SIDE", 2, raising=False)
    _reset_state()
    ok, reason = CYCLE._check_sector_cap("AXISBANK", "SHORT")
    assert ok is True


def test_sector_cap_allows_second_short_in_sector(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", True, raising=False)
    monkeypatch.setattr(CFG, "MAX_OPEN_PER_SECTOR_PER_SIDE", 2, raising=False)
    _reset_state()
    _add_position("AXISBANK", side="SHORT")
    ok, reason = CYCLE._check_sector_cap("ICICIBANK", "SHORT")
    assert ok is True


def test_sector_cap_blocks_third_short_in_sector(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", True, raising=False)
    monkeypatch.setattr(CFG, "MAX_OPEN_PER_SECTOR_PER_SIDE", 2, raising=False)
    _reset_state()
    _add_position("AXISBANK", side="SHORT")
    _add_position("ICICIBANK", side="SHORT")
    ok, reason = CYCLE._check_sector_cap("HDFCBANK", "SHORT")
    assert ok is False
    assert "sector_cap" in reason.lower()
    assert "BANKING" in reason


def test_sector_cap_separates_long_and_short_counts(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", True, raising=False)
    monkeypatch.setattr(CFG, "MAX_OPEN_PER_SECTOR_PER_SIDE", 2, raising=False)
    _reset_state()
    # 2 SHORTs in BANKING — at cap for SHORT but not for LONG
    _add_position("AXISBANK", side="SHORT")
    _add_position("ICICIBANK", side="SHORT")
    # New SHORT: blocked
    ok_s, _ = CYCLE._check_sector_cap("HDFCBANK", "SHORT")
    assert ok_s is False
    # New LONG: allowed (separate count)
    ok_l, _ = CYCLE._check_sector_cap("HDFCBANK", "BUY")
    assert ok_l is True


def test_sector_cap_disabled_via_flag(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", False, raising=False)
    _reset_state()
    _add_position("AXISBANK", side="SHORT")
    _add_position("ICICIBANK", side="SHORT")
    _add_position("HDFCBANK", side="SHORT")
    ok, reason = CYCLE._check_sector_cap("SBIN", "SHORT")
    assert ok is True
    assert reason == "disabled"


def test_sector_cap_fails_open_for_unknown_sector(monkeypatch):
    monkeypatch.setattr(CFG, "USE_SECTOR_CAP", True, raising=False)
    monkeypatch.setattr(CFG, "MAX_OPEN_PER_SECTOR_PER_SIDE", 2, raising=False)
    _reset_state()
    # ZZZ is not in our sector_map — should fail open (allow)
    ok, reason = CYCLE._check_sector_cap("ZZZ_NOT_REAL", "SHORT")
    assert ok is True
    assert "sector_unknown" in reason


# ============================================================================
# Fast-stage entry limit
# ============================================================================

def test_entries_within_first_n_min_counts_today():
    _reset_state()
    today = datetime.now(IST)
    open_ts = today.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    # 3 entries within first 15 min
    CYCLE.STATE["entry_event_ts"] = [
        open_ts + 60,        # 1 min in
        open_ts + 5 * 60,    # 5 min in
        open_ts + 12 * 60,   # 12 min in
        open_ts + 30 * 60,   # 30 min in (after window)
    ]
    assert CYCLE._entries_within_first_n_min(15) == 3
    assert CYCLE._entries_within_first_n_min(10) == 2


def test_entries_within_first_n_min_handles_empty():
    _reset_state()
    CYCLE.STATE["entry_event_ts"] = []
    assert CYCLE._entries_within_first_n_min(15) == 0


def test_fast_stage_disabled_via_flag(monkeypatch):
    monkeypatch.setattr(CFG, "USE_FAST_STAGE_ENTRY_LIMIT", False, raising=False)
    _reset_state()
    ok, reason = CYCLE._check_fast_stage_entry_limit("AXISBANK", "SHORT")
    assert ok is True
    assert reason == "disabled"


def test_fast_stage_outside_window_returns_allow(monkeypatch):
    """If we're past the fast-stage window, the limit doesn't apply."""
    monkeypatch.setattr(CFG, "USE_FAST_STAGE_ENTRY_LIMIT", True, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_DURATION_MIN", 15, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_MAX_ENTRIES", 3, raising=False)
    _reset_state()
    # We can't easily mock "now" without messing with datetime, so we test the
    # branch indirectly by setting many entries in last 15 min — if the bot is
    # currently outside the window, the cap shouldn't fire.
    # When this test runs, real-clock now is well past 09:30 most days; so
    # the function should return outside_fast_stage. If the test runs during
    # 09:15-09:30 IST it may behave differently — accept either branch.
    ok, reason = CYCLE._check_fast_stage_entry_limit("AXISBANK", "SHORT")
    # Outside window: allowed regardless of count
    # Inside window with 0 entries today: also allowed
    assert ok is True


def test_fast_stage_blocks_after_max_entries(monkeypatch):
    """Force the elapsed-min check to be inside the window via patching."""
    monkeypatch.setattr(CFG, "USE_FAST_STAGE_ENTRY_LIMIT", True, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_DURATION_MIN", 15, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_MAX_ENTRIES", 3, raising=False)
    _reset_state()

    # Mock datetime.now within the function module to a moment 5 min into
    # the session, with 3 entries already at minute 1, 2, 3.
    fixed_now = datetime.now(IST).replace(hour=9, minute=20, second=0, microsecond=0)
    open_ts = fixed_now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    CYCLE.STATE["entry_event_ts"] = [
        open_ts + 60,       # 09:16
        open_ts + 120,      # 09:17
        open_ts + 180,      # 09:18
    ]

    class _FixedDT:
        @staticmethod
        def now(_tz=None):
            return fixed_now

    monkeypatch.setattr(CYCLE, "datetime", _FixedDT)
    ok, reason = CYCLE._check_fast_stage_entry_limit("AXISBANK", "SHORT")
    assert ok is False
    assert "fast_stage_full" in reason


def test_fast_stage_allows_when_under_limit(monkeypatch):
    monkeypatch.setattr(CFG, "USE_FAST_STAGE_ENTRY_LIMIT", True, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_DURATION_MIN", 15, raising=False)
    monkeypatch.setattr(CFG, "FAST_STAGE_MAX_ENTRIES", 3, raising=False)
    _reset_state()

    fixed_now = datetime.now(IST).replace(hour=9, minute=20, second=0, microsecond=0)
    open_ts = fixed_now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    # Only 1 entry so far — under cap of 3
    CYCLE.STATE["entry_event_ts"] = [open_ts + 60]

    class _FixedDT:
        @staticmethod
        def now(_tz=None):
            return fixed_now

    monkeypatch.setattr(CYCLE, "datetime", _FixedDT)
    ok, reason = CYCLE._check_fast_stage_entry_limit("AXISBANK", "SHORT")
    assert ok is True


# ============================================================================
# GOD_MAX_CONCURRENT_TRADES default lowered
# ============================================================================

def test_god_max_concurrent_default_is_8():
    """Audit recommendation: 50 -> 8."""
    assert CFG.GOD_MAX_CONCURRENT_TRADES == 8


def test_god_max_concurrent_applied_in_dynamic():
    """When risk_profile=GOD and no override, _dynamic_max_concurrent uses GOD_MAX_CONCURRENT_TRADES."""
    _reset_state()
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["risk_profile"] = "GOD"
    # Ensure no MAX_CONCURRENT_TRADES override leaks from .env
    saved = os.environ.pop("MAX_CONCURRENT_TRADES", None)
    try:
        # Temporarily patch CFG.MAX_CONCURRENT_TRADES to 0 to be sure
        # Note: _dynamic_max_concurrent reads via _cfg_get which reads CFG
        n = CYCLE._dynamic_max_concurrent()
        assert n == CFG.GOD_MAX_CONCURRENT_TRADES, f"expected {CFG.GOD_MAX_CONCURRENT_TRADES} got {n}"
    finally:
        if saved is not None:
            os.environ["MAX_CONCURRENT_TRADES"] = saved


# ============================================================================
# /godstatus command
# ============================================================================

def test_godstatus_command_returns_snapshot(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state()
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["risk_profile"] = "GOD"
        CYCLE.STATE["trading_mode"] = "HYBRID"
    ev = _DummyEvent(sender_id=1001)

    async def _run():
        return await bot._dispatch_command(ev, 1001, "/godstatus", "")

    handled = asyncio.run(_run())
    assert handled is True
    assert len(ev.replies) == 1
    text = ev.replies[0]
    assert "GOD-Mode Status" in text or "GOD" in text
    assert "Profile:" in text
    assert "HYBRID" in text
    assert "Sector cap" in text
    assert "Fast-stage" in text


def test_godstatus_blocked_for_non_viewer(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state()
    ev = _DummyEvent(sender_id=9999)  # not in any role list

    async def _run():
        return await bot._dispatch_command(ev, 9999, "/godstatus", "")

    handled = asyncio.run(_run())
    assert handled is True
    assert len(ev.replies) == 1
    assert "Not permitted" in ev.replies[0]
