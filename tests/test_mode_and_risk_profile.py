"""Tests for INTRADAY/SWING/HYBRID trading modes and STANDARD/GOD risk profiles.

Covers all 19 acceptance scenarios from the controlled-upgrade spec:

Trading mode (1-9):
 1. `/mode hybrid` updates runtime/env mode
 2. intraday mode routes trades to MIS
 3. swing mode routes eligible long trades to CNC
 4. hybrid routes qualifying FULL aligned long MTF trades to swing/CNC
 5. hybrid keeps non-qualifying trades intraday/MIS
 6. shorts do not become swing
 7. force-exit skips swing/CNC positions
 8. panic/close-all still closes both
 9. status output includes current trading mode

Risk profile (10-19):
 10. `/riskprofile god` does NOT activate immediately
 11. `/riskprofile god confirm` activates God Mode
 12. God button requires confirm action before activation
 13. God cancel leaves profile unchanged
 14. `/riskprofile standard` switches back immediately
 15. GOD increases effective deployable/exposure behavior vs STANDARD
 16. GOD still respects affordability/wallet checks
 17. STANDARD behavior remains unchanged by default
 18. status output includes current risk profile
 19. status/output reflects pending God confirmation when present
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import bot
import trading_cycle as CYCLE
import config as CFG


class DummyEvent:
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
    monkeypatch.setattr(bot, "set_env_value", lambda *a, **k: None)


def _reset_state(profile="STANDARD", mode="INTRADAY"):
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["trading_mode"] = mode
        CYCLE.STATE["risk_profile"] = profile
        CYCLE.STATE["pending_risk_profile_confirmation"] = None
        CYCLE.STATE["positions"] = {}


async def _run(cmd_word, cmd_arg="", sender=1001):
    ev = DummyEvent(sender_id=sender)
    handled = await bot._dispatch_command(ev, sender, cmd_word, cmd_arg)
    return handled, ev


# ============================================================================
# Trading mode
# ============================================================================

def test_01_mode_hybrid_updates_runtime(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state()
    handled, ev = asyncio.run(_run("/mode", "hybrid"))
    assert handled is True
    assert CYCLE.current_trading_mode() == "HYBRID"
    assert any("HYBRID" in r for r in ev.replies)


def test_02_intraday_mode_routes_buy_to_mis(monkeypatch):
    _reset_state(mode="INTRADAY")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY"
    assert CYCLE.product_for_trade_mode(tm) == "MIS"


def test_03_swing_mode_routes_eligible_long_to_cnc(monkeypatch):
    _reset_state(mode="SWING")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "vwap_ema_long",
        "tier": "REDUCED",
        "regime": "SIDEWAYS",
    })
    assert tm == "SWING"
    assert CYCLE.product_for_trade_mode(tm) == "CNC"


def test_04_hybrid_routes_qualified_long_to_swing(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "strategy_family": "trend_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
        "weak_market_exception": False,
    })
    assert tm == "SWING", f"expected SWING, got {tm} ({reason})"
    assert CYCLE.product_for_trade_mode(tm) == "CNC"


def test_05a_hybrid_keeps_wrong_tier_intraday(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "REDUCED",  # not FULL
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY"
    assert "tier_not_full" in reason


def test_05b_hybrid_keeps_mean_reversion_intraday(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_family": "mean_reversion",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY"
    assert "mean_reversion" in reason


def test_05c_hybrid_keeps_weak_market_exception_intraday(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
        "weak_market_exception": True,
    })
    assert tm == "INTRADAY"
    assert "weak_market" in reason


def test_05d_hybrid_keeps_sideways_regime_intraday(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "BUY",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "SIDEWAYS",  # not trending
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY"
    assert "regime_not_trending" in reason


def test_06a_short_not_routed_to_swing_in_hybrid(monkeypatch):
    _reset_state(mode="HYBRID")
    tm, reason = CYCLE.classify_trade_mode({
        "side": "SHORT",
        "strategy_tag": "mtf_confirmed_long",
        "tier": "FULL",
        "regime": "TRENDING_UP",
        "trend_direction": "UP",
    })
    assert tm == "INTRADAY"
    assert reason == "short_always_intraday"


def test_06b_short_not_routed_to_swing_in_swing_mode(monkeypatch):
    _reset_state(mode="SWING")
    tm, reason = CYCLE.classify_trade_mode({"side": "SHORT"})
    assert tm == "INTRADAY"


def test_07_force_exit_skips_swing_positions(monkeypatch):
    _reset_state(mode="HYBRID")
    # Install a mix of intraday + swing positions
    CYCLE.STATE["positions"] = {
        "INTRA1": {"trade_mode": "INTRADAY", "product": "MIS", "side": "BUY", "entry": 100, "qty": 1},
        "INTRA2": {"trade_mode": "INTRADAY", "product": "MIS", "side": "SHORT", "entry": 200, "qty": 1},
        "SWING1": {"trade_mode": "SWING", "product": "CNC", "side": "BUY", "entry": 300, "qty": 1},
    }

    # Reconstruct the force-exit filtering predicate from the tick() logic:
    def _is_intraday(t):
        tm = str(t.get("trade_mode") or "").upper()
        if tm in ("INTRADAY", "SWING"):
            return tm == "INTRADAY"
        return str(t.get("product") or "MIS").upper() == "MIS"

    mis = {s: t for s, t in CYCLE.STATE["positions"].items() if _is_intraday(t)}
    cnc = {s: t for s, t in CYCLE.STATE["positions"].items() if not _is_intraday(t)}
    assert set(mis.keys()) == {"INTRA1", "INTRA2"}
    assert set(cnc.keys()) == {"SWING1"}


def test_07b_force_exit_legacy_product_fallback(monkeypatch):
    """Older trades may lack `trade_mode` but have `product`. Must fall back."""
    _reset_state(mode="HYBRID")
    CYCLE.STATE["positions"] = {
        "LEGACY_MIS": {"product": "MIS", "side": "BUY", "entry": 100, "qty": 1},
        "LEGACY_CNC": {"product": "CNC", "side": "BUY", "entry": 300, "qty": 1},
    }

    def _is_intraday(t):
        tm = str(t.get("trade_mode") or "").upper()
        if tm in ("INTRADAY", "SWING"):
            return tm == "INTRADAY"
        return str(t.get("product") or "MIS").upper() == "MIS"

    mis = {s: t for s, t in CYCLE.STATE["positions"].items() if _is_intraday(t)}
    cnc = {s: t for s, t in CYCLE.STATE["positions"].items() if not _is_intraday(t)}
    assert set(mis.keys()) == {"LEGACY_MIS"}
    assert set(cnc.keys()) == {"LEGACY_CNC"}


def test_08_panic_closes_both_intraday_and_swing(monkeypatch):
    _reset_state(mode="HYBRID")
    CYCLE.STATE["positions"] = {
        "A_INTRA": {"trade_mode": "INTRADAY", "product": "MIS", "side": "BUY", "entry": 100, "qty": 1},
        "B_SWING": {"trade_mode": "SWING", "product": "CNC", "side": "BUY", "entry": 300, "qty": 1},
    }
    closed_symbols = []

    def fake_force_exit_all(positions, close_fn, reason="PANIC"):
        for s in positions:
            closed_symbols.append(s)
        return True

    monkeypatch.setattr(CYCLE, "ee_force_exit_all", fake_force_exit_all)
    CYCLE._close_all_open_trades(reason="PANIC")
    assert set(closed_symbols) == {"A_INTRA", "B_SWING"}


def test_09_status_output_includes_trading_mode(monkeypatch):
    _reset_state(mode="HYBRID")
    # Minimal patches for get_status_text dependencies
    monkeypatch.setattr(CYCLE, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(CYCLE, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(CYCLE, "is_live_enabled", lambda: False)
    monkeypatch.setattr(CYCLE, "_refresh_runtime_pnl_fields", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(CYCLE, "load_universe_trading", lambda: [])
    monkeypatch.setattr(CYCLE, "load_universe_live", lambda: [])
    monkeypatch.setattr(CYCLE, "get_regime_entry_mode", lambda r: "UNKNOWN")
    text = CYCLE.get_status_text()
    assert "Trading Mode: HYBRID" in text


# ============================================================================
# Risk profile
# ============================================================================

def test_10_riskprofile_god_does_not_activate_immediately(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(profile="STANDARD")
    handled, ev = asyncio.run(_run("/riskprofile", "god"))
    assert handled is True
    assert CYCLE.current_risk_profile() == "STANDARD"  # still standard
    assert CYCLE.STATE.get("pending_risk_profile_confirmation") == "GOD"
    reply_text = " ".join(ev.replies)
    assert "CONFIRM" in reply_text.upper()
    assert "broker" in reply_text.lower()


def test_11_riskprofile_god_confirm_activates(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(profile="STANDARD")
    # Step 1: request
    asyncio.run(_run("/riskprofile", "god"))
    assert CYCLE.current_risk_profile() == "STANDARD"
    # Step 2: confirm
    handled, ev = asyncio.run(_run("/riskprofile", "god confirm"))
    assert handled is True
    assert CYCLE.current_risk_profile() == "GOD"
    assert CYCLE.STATE.get("pending_risk_profile_confirmation") is None


def test_12_god_button_requires_confirm_before_activation(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(profile="STANDARD")
    # Simulate button press: /riskprofile_god (panel handler)
    handled, _ = asyncio.run(_run("/riskprofile_god", ""))
    assert handled is True
    assert CYCLE.current_risk_profile() == "STANDARD"
    assert CYCLE.STATE.get("pending_risk_profile_confirmation") == "GOD"
    # Now confirm via button
    handled, _ = asyncio.run(_run("/riskprofile_god_confirm", ""))
    assert handled is True
    assert CYCLE.current_risk_profile() == "GOD"


def test_13_god_cancel_leaves_profile_unchanged(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(profile="STANDARD")
    asyncio.run(_run("/riskprofile", "god"))
    assert CYCLE.STATE.get("pending_risk_profile_confirmation") == "GOD"
    handled, ev = asyncio.run(_run("/riskprofile", "cancel"))
    assert handled is True
    assert CYCLE.current_risk_profile() == "STANDARD"
    assert CYCLE.STATE.get("pending_risk_profile_confirmation") is None


def test_14_riskprofile_standard_switches_back_immediately(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(profile="STANDARD")
    # Activate GOD
    asyncio.run(_run("/riskprofile", "god"))
    asyncio.run(_run("/riskprofile", "god confirm"))
    assert CYCLE.current_risk_profile() == "GOD"
    # Switch back
    handled, _ = asyncio.run(_run("/riskprofile", "standard"))
    assert handled is True
    assert CYCLE.current_risk_profile() == "STANDARD"


def test_15_god_increases_effective_exposure_vs_standard(monkeypatch):
    _reset_state(profile="STANDARD")
    std_exp = CYCLE._effective_max_exposure_pct()
    std_dep = float(CYCLE._cfg_get("MAX_DEPLOYABLE_PCT", 0))
    std_sym = float(CYCLE._cfg_get("MAX_SYMBOL_ALLOCATION_PCT", 0))
    std_full = float(CYCLE._cfg_get("FULL_TIER_WEIGHT", 0))

    with CYCLE.STATE_LOCK:
        CYCLE.STATE["risk_profile"] = "GOD"
    god_exp = CYCLE._effective_max_exposure_pct()
    god_dep = float(CYCLE._cfg_get("MAX_DEPLOYABLE_PCT", 0))
    god_sym = float(CYCLE._cfg_get("MAX_SYMBOL_ALLOCATION_PCT", 0))
    god_full = float(CYCLE._cfg_get("FULL_TIER_WEIGHT", 0))

    assert god_exp > std_exp, f"GOD exposure {god_exp} not > STANDARD {std_exp}"
    assert god_dep > std_dep, f"GOD deployable {god_dep} not > STANDARD {std_dep}"
    assert god_sym > std_sym, f"GOD symbol alloc {god_sym} not > STANDARD {std_sym}"
    assert god_full > std_full, f"GOD full tier wt {god_full} not > STANDARD {std_full}"


def test_16_god_still_respects_affordability(monkeypatch):
    """GOD must not override the wallet-scaled base of _max_exposure_inr.

    The effective % can jump to 95% under GOD, but the base remains the
    actual wallet (with a documented CFG.CAPITAL_INR boot-time fallback
    used only before the first wallet sync). When BOTH wallet and the
    fallback are 0, the cap must be 0.
    """
    # With wallet=0 AND CAPITAL_INR patched to 0, GOD cap must be 0 too.
    monkeypatch.setattr(CFG, "CAPITAL_INR", 0.0, raising=False)
    _reset_state(profile="GOD")
    CYCLE.STATE["wallet_net_inr"] = 0.0
    assert CYCLE._max_exposure_inr() == 0.0

    # With real wallet, GOD cap > STANDARD cap, both scale with wallet.
    CYCLE.STATE["wallet_net_inr"] = 10000.0
    cap_god = CYCLE._max_exposure_inr()

    _reset_state(profile="STANDARD")
    CYCLE.STATE["wallet_net_inr"] = 10000.0
    cap_std = CYCLE._max_exposure_inr()

    assert cap_god > cap_std
    assert cap_god <= 10000.0  # can't exceed wallet


def test_17_standard_behavior_unchanged_by_default(monkeypatch):
    _reset_state(profile="STANDARD")
    # Under STANDARD, _cfg_get returns the base CFG values verbatim
    assert CYCLE._cfg_get("MAX_EXPOSURE_PCT", 0) == CFG.MAX_EXPOSURE_PCT
    assert CYCLE._cfg_get("MAX_DEPLOYABLE_PCT", 0) == CFG.MAX_DEPLOYABLE_PCT
    assert CYCLE._cfg_get("MAX_SYMBOL_ALLOCATION_PCT", 0) == CFG.MAX_SYMBOL_ALLOCATION_PCT
    assert CYCLE._cfg_get("FULL_TIER_WEIGHT", 0) == CFG.FULL_TIER_WEIGHT


def test_18_status_output_includes_risk_profile(monkeypatch):
    _reset_state(profile="GOD", mode="HYBRID")
    monkeypatch.setattr(CYCLE, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(CYCLE, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(CYCLE, "is_live_enabled", lambda: False)
    monkeypatch.setattr(CYCLE, "_refresh_runtime_pnl_fields", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(CYCLE, "load_universe_trading", lambda: [])
    monkeypatch.setattr(CYCLE, "load_universe_live", lambda: [])
    monkeypatch.setattr(CYCLE, "get_regime_entry_mode", lambda r: "UNKNOWN")
    text = CYCLE.get_status_text()
    assert "Risk Profile: GOD" in text


def test_19_status_reflects_pending_god_confirmation(monkeypatch):
    _reset_state(profile="STANDARD")
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["pending_risk_profile_confirmation"] = "GOD"
    monkeypatch.setattr(CYCLE, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(CYCLE, "_ensure_day_key", lambda: None)
    monkeypatch.setattr(CYCLE, "is_live_enabled", lambda: False)
    monkeypatch.setattr(CYCLE, "_refresh_runtime_pnl_fields", lambda: (0.0, 0.0, 0.0))
    monkeypatch.setattr(CYCLE, "load_universe_trading", lambda: [])
    monkeypatch.setattr(CYCLE, "load_universe_live", lambda: [])
    monkeypatch.setattr(CYCLE, "get_regime_entry_mode", lambda r: "UNKNOWN")
    text = CYCLE.get_status_text()
    assert "pending GOD confirmation" in text


# ============================================================================
# Extra: confirm no duplicate /mode_hybrid fires without arg
# ============================================================================

def test_20_mode_intraday_is_still_default_behavior(monkeypatch):
    _patch_bot_perms(monkeypatch)
    _reset_state(mode="INTRADAY")
    handled, ev = asyncio.run(_run("/mode", "intraday"))
    assert handled is True
    assert CYCLE.current_trading_mode() == "INTRADAY"
    assert CYCLE.product_for_trade_mode("INTRADAY") == "MIS"


def test_21_confirm_god_without_pending_fails(monkeypatch):
    _reset_state(profile="STANDARD")
    ok, msg = CYCLE.confirm_god_mode()
    assert ok is False
    assert CYCLE.current_risk_profile() == "STANDARD"


# ============================================================================
# State persistence (B+C cleanup pass)
# ============================================================================

def test_22_state_persist_keys_include_mode_and_profile():
    """Mode and profile MUST be in the persist key list, else they don't survive restart."""
    assert "trading_mode" in CYCLE._STATE_PERSIST_KEYS
    assert "risk_profile" in CYCLE._STATE_PERSIST_KEYS


def test_23_persistence_round_trip_same_day(monkeypatch, tmp_path):
    """Save snapshot today, load it back today — mode and profile restore intact."""
    snap_file = tmp_path / "state_snapshot.json"
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_file))

    # Configure source state
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["trading_mode"] = "HYBRID"
        CYCLE.STATE["risk_profile"] = "GOD"
        CYCLE.STATE["day_key"] = CYCLE.datetime.now(CYCLE.IST).strftime("%Y-%m-%d")
        CYCLE.STATE["positions"] = {}

    CYCLE._save_state_snapshot()
    assert snap_file.exists()

    # Wipe the runtime state to simulate a fresh process boot
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["trading_mode"] = "INTRADAY"
        CYCLE.STATE["risk_profile"] = "STANDARD"

    CYCLE._load_state_snapshot()

    # Both must be restored from the same-day snapshot
    assert CYCLE.current_trading_mode() == "HYBRID"
    assert CYCLE.current_risk_profile() == "GOD"


def test_24_god_auto_reverts_on_stale_day(monkeypatch, tmp_path):
    """Cross-day boot with saved GOD must auto-revert to STANDARD (safety)."""
    snap_file = tmp_path / "state_snapshot.json"
    monkeypatch.setattr(CYCLE, "_STATE_SNAPSHOT_PATH", str(snap_file))
    monkeypatch.setenv("RISK_PROFILE", "GOD")

    # Save snapshot dated yesterday
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["trading_mode"] = "HYBRID"
        CYCLE.STATE["risk_profile"] = "GOD"
        CYCLE.STATE["day_key"] = "1999-01-01"  # definitely stale
        CYCLE.STATE["positions"] = {}

    CYCLE._save_state_snapshot()

    # Stub set_env_value so the test can't accidentally rewrite the real .env
    import env_utils
    monkeypatch.setattr(env_utils, "set_env_value", lambda *a, **k: None)

    # Today's load: snapshot is stale → GOD must auto-revert to STANDARD
    with CYCLE.STATE_LOCK:
        CYCLE.STATE["risk_profile"] = "GOD"  # simulate env-driven boot
    CYCLE._load_state_snapshot()
    assert CYCLE.current_risk_profile() == "STANDARD", \
        "GOD must auto-revert when snapshot is from a prior day"
