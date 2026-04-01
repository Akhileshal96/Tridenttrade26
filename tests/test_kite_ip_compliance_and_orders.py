import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def _reset_ip_state():
    tc.STATE["paused"] = False
    tc.STATE["live_override"] = True
    tc.STATE["ip_current"] = ""
    tc.STATE["ip_expected"] = ""
    tc.STATE["ip_compliant"] = False
    tc.STATE["ip_last_error"] = ""
    tc.STATE["ip_last_check_ts"] = 0.0
    tc.STATE["ip_manual_rearm_required"] = False
    tc.STATE["live_order_allowed"] = False


def test_startup_ip_match_marks_compliant(monkeypatch):
    _reset_ip_state()
    monkeypatch.setattr(tc, "_fetch_public_ipv4", lambda timeout_sec=3.0: "1.2.3.4")
    monkeypatch.setattr(tc, "_cfg_get", lambda k, d=None: "1.2.3.4" if k == "KITE_STATIC_IP" else d)

    ok = tc.evaluate_ip_compliance(force=True)

    assert ok is True
    assert tc.STATE["ip_compliant"] is True
    assert tc.STATE["ip_current"] == "1.2.3.4"


def test_startup_ip_mismatch_blocks_live(monkeypatch):
    _reset_ip_state()
    monkeypatch.setattr(tc, "_fetch_public_ipv4", lambda timeout_sec=3.0: "5.6.7.8")
    monkeypatch.setattr(tc, "_cfg_get", lambda k, d=None: "1.2.3.4" if k == "KITE_STATIC_IP" else d)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    ok = tc.evaluate_ip_compliance(force=True)

    assert ok is False
    assert tc.STATE["ip_compliant"] is False
    assert tc.STATE["live_order_allowed"] is False
    assert tc.STATE["paused"] is True
    assert tc.STATE["live_override"] is False


def test_live_order_blocked_on_ip_mismatch(monkeypatch):
    _reset_ip_state()
    tc.STATE["live_order_allowed"] = False
    monkeypatch.setattr(tc, "evaluate_ip_compliance", lambda force=False: False)
    monkeypatch.setattr(tc, "append_log", lambda *a, **k: None)

    class DummyKite:
        def place_order(self, **kwargs):
            raise AssertionError("place_order should not be called when IP is non-compliant")

    out = tc._place_live_order(DummyKite(), "SBIN", "BUY", 1)
    assert out is None


def test_market_order_includes_market_protection(monkeypatch):
    _reset_ip_state()
    tc.STATE["live_order_allowed"] = True
    monkeypatch.setattr(tc, "evaluate_ip_compliance", lambda force=False: True)
    monkeypatch.setattr(tc, "_order_rate_limit_wait", lambda: None)
    monkeypatch.setattr(
        tc,
        "_cfg_get",
        lambda k, d=None: (0.25 if k == "MARKET_PROTECTION" else d),
    )

    got = {}

    class DummyKite:
        VARIETY_REGULAR = "regular"
        TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"
        ORDER_TYPE_MARKET = "MARKET"

        def place_order(self, **kwargs):
            got.update(kwargs)
            return "OID42"

    oid = tc._place_live_order(DummyKite(), "SBIN", "BUY", 2)
    assert oid == "OID42"
    assert float(got.get("market_protection") or 0.0) > 0.0


def test_order_path_retries_on_429_with_backoff(monkeypatch):
    _reset_ip_state()
    tc.STATE["live_order_allowed"] = True
    monkeypatch.setattr(tc, "evaluate_ip_compliance", lambda force=False: True)
    monkeypatch.setattr(tc, "_order_rate_limit_wait", lambda: None)
    monkeypatch.setattr(
        tc,
        "_cfg_get",
        lambda k, d=None: (0.2 if k == "MARKET_PROTECTION" else d),
    )

    sleeps = []
    monkeypatch.setattr(tc.time, "sleep", lambda s: sleeps.append(s))

    class DummyKite:
        VARIETY_REGULAR = "regular"
        TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"
        ORDER_TYPE_MARKET = "MARKET"

        def __init__(self):
            self.calls = 0

        def place_order(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise Exception("429 Too Many Requests")
            return "OID-Retry"

    kite = DummyKite()
    oid = tc._place_live_order(kite, "SBIN", "BUY", 1)

    assert oid == "OID-Retry"
    assert kite.calls == 2
    assert len(sleeps) >= 1
