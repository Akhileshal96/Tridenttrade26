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
