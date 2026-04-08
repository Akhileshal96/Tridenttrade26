import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def reset_state():
    tc.STATE["positions"] = {}
    tc.STATE["open_trades"] = tc.STATE["positions"]
    tc.STATE["today_pnl"] = 0.0
    tc.STATE["cooldown_until"] = None
    tc.STATE["last_exit_ts"] = {}
    tc.STATE["paused"] = False
    tc.STATE["initiated"] = False
    tc.STATE["live_override"] = False
    tc.STATE["loss_streak"] = 0
    tc.STATE["reduce_size_factor"] = 1.0
    tc.STATE["pause_entries_until"] = None
    tc.STATE["halt_for_day"] = False
    tc.STATE["day_peak_pnl"] = 0.0


def test_close_position_paper_updates_pnl_and_removes_position(monkeypatch):
    reset_state()
    tc.STATE["positions"]["ABC"] = {"entry_price": 100.0, "quantity": 10}

    monkeypatch.setattr(tc, "_set_cooldown", lambda: None)
    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc, "_notify", lambda *args, **kwargs: None)

    ok = tc._close_position("ABC", reason="TEST", ltp_override=95.0)

    assert ok is True
    assert "ABC" not in tc.STATE["positions"]
    assert tc.STATE["today_pnl"] == -50.0
    assert "ABC" in tc.STATE["last_exit_ts"]


def test_monitor_positions_hits_stoploss_and_sells(monkeypatch):
    reset_state()
    tc.STATE["positions"]["ABC"] = {"entry_price": 100.0, "quantity": 10, "peak_pct": 0.0, "trailing_active": False}

    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)

    # Force LTP down to -2% threshold breach
    monkeypatch.setattr(tc, "_ltp", lambda kite, sym: 98.0)
    monkeypatch.setattr(tc, "is_live_enabled", lambda: True)
    monkeypatch.setattr(tc, "get_kite", lambda: object())

    closed = {"called": False, "reason": None}

    def fake_close(sym, reason="MANUAL", ltp_override=None):
        closed["called"] = True
        closed["reason"] = reason
        tc.STATE["positions"].pop(sym, None)
        return True

    monkeypatch.setattr(tc, "_close_position", fake_close)

    tc.ee_monitor_positions(
        tc.STATE,
        tc.STATE["positions"],
        get_ltp=lambda sym: tc._ltp(tc.get_kite(), sym),
        close_position=tc._close_position,
        force_exit_check=tc._past_force_exit_time,
    )

    assert closed["called"] is True
    assert closed["reason"] == "SL"


def test_monitor_positions_trailing_exit(monkeypatch):
    reset_state()
    tc.STATE["positions"]["ABC"] = {
        "entry_price": 100.0,
        "quantity": 10,
        "peak_pnl_inr": 20.0,
        "trailing_active": True,
    }

    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc, "_ltp", lambda kite, sym: 100.2)  # pnl_inr = 2.0
    monkeypatch.setattr(tc, "is_live_enabled", lambda: True)
    monkeypatch.setattr(tc, "get_kite", lambda: object())

    monkeypatch.setattr(tc.CFG, "TRAIL_LOCK_RATIO", 0.5, raising=False)
    monkeypatch.setattr(tc.CFG, "TRAIL_BUFFER_INR", 1.0, raising=False)

    closed = {"reason": None}

    def fake_close(sym, reason="MANUAL", ltp_override=None):
        closed["reason"] = reason
        tc.STATE["positions"].pop(sym, None)
        return True

    monkeypatch.setattr(tc, "_close_position", fake_close)

    tc.ee_monitor_positions(
        tc.STATE,
        tc.STATE["positions"],
        get_ltp=lambda sym: tc._ltp(tc.get_kite(), sym),
        close_position=tc._close_position,
        force_exit_check=tc._past_force_exit_time,
    )

    assert closed["reason"] == "TRAIL"


def test_can_open_new_trade_uses_full_notional(monkeypatch):
    reset_state()
    tc.STATE["wallet_available_inr"] = 500.0
    tc.STATE["wallet_net_inr"] = 500.0
    tc.RUNTIME["MAX_EXPOSURE_PCT"] = 100.0

    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc.CFG, "MAX_EXPOSURE_PCT", 100.0, raising=False)

    # entry*qty = 100*6 = 600 > 500 should block
    assert tc._can_open_new_trade("ABC", 100.0, qty=6) is False
    # entry*qty = 400 <= 500 should pass (assuming no other blockers)
    assert tc._can_open_new_trade("ABC", 100.0, qty=4) is True


def test_reentry_block_allows_positive_momentum(monkeypatch):
    reset_state()
    tc.STATE["wallet_available_inr"] = 10000.0
    tc.STATE["wallet_net_inr"] = 10000.0
    tc.STATE["last_exit_ts"] = {"ABC": tc.datetime.now(tc.IST)}

    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc.CFG, "REENTRY_BLOCK_MINUTES", 30, raising=False)

    assert tc._can_open_new_trade("ABC", 100.0, qty=1, momentum_positive=False) is False
    assert tc._can_open_new_trade("ABC", 100.0, qty=1, momentum_positive=True) is True


def test_close_position_short_live_uses_buy_to_cover(monkeypatch):
    reset_state()
    tc.STATE["positions"]["ABC"] = {"entry_price": 100.0, "quantity": 2, "side": "SHORT"}

    monkeypatch.setattr(tc, "is_live_enabled", lambda: True)
    monkeypatch.setattr(tc, "get_kite", lambda: object())
    monkeypatch.setattr(tc, "_ltp", lambda kite, sym: 95.0)
    monkeypatch.setattr(tc, "_set_cooldown", lambda: None)
    monkeypatch.setattr(tc, "append_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc, "_notify", lambda *args, **kwargs: None)

    placed = {"side": None}

    def fake_place(kite, sym, side, qty):
        placed["side"] = side
        return "OID1"

    monkeypatch.setattr(tc, "_place_live_order", fake_place)

    ok = tc._close_position("ABC", reason="TEST", ltp_override=95.0)

    assert ok is True
    assert placed["side"] == "BUY"


def test_run_loop_forever_logs_current_weak_config(monkeypatch):
    calls = []

    monkeypatch.setattr(tc, "append_log", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(tc, "_active_trade_universe", lambda: [])
    monkeypatch.setattr(tc, "_load_research_universe_from_file", lambda: [])

    hit = {"n": 0}

    def fake_tick():
        hit["n"] += 1
        raise KeyboardInterrupt()

    monkeypatch.setattr(tc, "tick", fake_tick)

    try:
        tc.run_loop_forever()
    except KeyboardInterrupt:
        pass

    market_logs = [x for x in calls if len(x) >= 3 and x[1] == "MARKET"]
    assert market_logs
    assert "min_score=0.75" in market_logs[0][2]


def test_tick_post_0930_path_does_not_raise_when_cfg_binding_missing(monkeypatch):
    reset_state()
    tc.STATE["paused"] = False
    tc.STATE["halt_for_day"] = False
    tc.STATE["today_pnl"] = 0.0
    tc.STATE["daily_loss_cap_inr"] = 0.0
    tc.STATE["daily_profit_milestone_inr"] = 0.0

    monkeypatch.delattr(tc, "CFG", raising=False)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "evaluate_ip_compliance", lambda force=False: None)
    monkeypatch.setattr(tc.RISK, "sync_wallet", lambda _s: None)
    monkeypatch.setattr(tc, "_sync_wallet_and_caps", lambda force=False: None)
    monkeypatch.setattr(tc, "reconcile_broker_positions", lambda: None)
    monkeypatch.setattr(tc, "_refresh_runtime_pnl_fields", lambda: None)
    monkeypatch.setattr(tc.RISK, "check_day_drawdown_guard", lambda _s: None)
    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "_positions", lambda: {})
    monkeypatch.setattr(tc, "_in_any_promote_window", lambda: False)
    monkeypatch.setattr(tc, "_cooldown_ok", lambda: False)
    monkeypatch.setattr(tc, "_within_entry_window", lambda: True)
    monkeypatch.setattr(tc, "_resolve_trade_universe", lambda: ["INFY"])
    monkeypatch.setattr(tc, "refresh_active_universe_if_due", lambda _u: ["INFY"])
    monkeypatch.setattr(tc, "_open_positions_count", lambda: 0)
    monkeypatch.setattr(tc, "get_market_regime_snapshot", lambda: {"regime": "SIDEWAYS", "trend_direction": "UNKNOWN"})
    monkeypatch.setattr(tc, "_is_micro_mode_active", lambda: False)
    monkeypatch.setattr(tc, "get_opening_mode", lambda: ("OPEN_CLEAN", {"confidence": 60, "reason": "ok"}))
    monkeypatch.setattr(tc, "_in_open_filter_window", lambda: False)
    monkeypatch.setattr(tc, "_maybe_refresh_active_strategy_families", lambda *a, **k: ["mean_reversion"])
    monkeypatch.setattr(tc, "_scan_top3_families", lambda *a, **k: 0)
    monkeypatch.setattr(tc, "_record_research_event", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_refresh_active_strategy_families", lambda *a, **k: ["mean_reversion"])

    tc.tick()


def test_tick_uses_single_wallet_sync_path(monkeypatch):
    reset_state()
    tc.STATE["paused"] = True

    calls = {"n": 0}

    def fake_sync_wallet_and_caps(force=False):
        calls["n"] += 1

    monkeypatch.setattr(tc, "_sync_wallet_and_caps", fake_sync_wallet_and_caps)
    monkeypatch.setattr(tc, "evaluate_ip_compliance", lambda force=False: None)
    monkeypatch.setattr(tc, "reconcile_broker_positions", lambda: None)
    monkeypatch.setattr(tc, "_refresh_runtime_pnl_fields", lambda: None)
    monkeypatch.setattr(tc.RISK, "check_day_drawdown_guard", lambda _s: None)
    monkeypatch.setattr(tc, "_past_force_exit_time", lambda: False)
    monkeypatch.setattr(tc, "_positions", lambda: {})
    monkeypatch.setattr(tc, "_maybe_send_eod_report", lambda: None)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    def fail_sync_wallet(_state):
        raise AssertionError("RISK.sync_wallet should not be called from tick")

    monkeypatch.setattr(tc.RISK, "sync_wallet", fail_sync_wallet)

    tc.tick()
    assert calls["n"] == 1


def test_wallet_off_market_auth_error_enters_cached_mode(monkeypatch):
    reset_state()
    tc.STATE["last_wallet"] = 1234.0
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(tc, "_is_market_hours", lambda _now: False)
    monkeypatch.setattr(tc, "_cached_wallet_value", lambda: 1234.0)
    monkeypatch.setattr(tc, "get_kite", lambda: type("K", (), {"margins": lambda self: (_ for _ in ()).throw(Exception("invalid token"))})())
    monkeypatch.setattr(tc.CFG, "WALLET_AUTH_COOLDOWN_SEC", 600, raising=False)

    tc._sync_wallet_and_caps(force=True)
    assert tc.STATE.get("wallet_cached_mode_until") is not None
    assert float(tc.STATE.get("wallet_net_inr") or 0.0) == 1234.0
