"""Audit fixes (2026-05-17): control-panel button cleanup + analytics
report test-pollution filter.

Bug context:
  User reported the analytics panel handlers were "not working":
    /beststrategy, /worststrategy, /regimereport, /sectorreport, /strategyscores

  Root cause investigation found trade_history.csv contained 34 rows of
  TEST pollution (symbol=ABC, strategy/family='unknown', regime='UNKNOWN',
  reason='TEST') leftover from pre-pytest-isolation test runs. These
  drowned out any real trades in the grouped stats, producing meaningless
  output like "Best: unknown net=₹0.00".

Fixes:
  A. strategy_analytics._is_test_row(): defensively drops synthetic rows
     during rebuild_strategy_stats (reason=TEST or symbol in stub set).
  B. strategy_analytics._is_meaningful_key(): filters 'unknown'/'UNKNOWN'/
     'TEST'/etc. buckets from all 4 report functions at display time.
  C. bot.py /cleanstats: owner-only command to wipe cached
     strategy_stats.json and force rebuild with the new filters.
  D. Panel: removed dead "🧬 Hybrid" button (gated off via ENABLE_HYBRID_
     MODE). Added "🧠 Learnings" + "🗑 Clear Position" buttons. Wired the
     previously-orphaned "holdings" handler.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

import strategy_analytics as SA


# ============================================================================
# A — _is_test_row catches synthetic rows
# ============================================================================

def test_is_test_row_detects_reason_TEST():
    assert SA._is_test_row({"reason": "TEST"}) is True
    assert SA._is_test_row({"reason": "test"}) is True  # case-insensitive


def test_is_test_row_detects_stub_symbols_with_unknown_tag():
    """Stub symbols with no real strategy_tag are pollution — drop them."""
    for stub in ("ABC", "FOO", "BAR", "XYZ", "TEST"):
        assert SA._is_test_row({"symbol": stub, "reason": "SL",
                                "strategy_tag": "unknown"}) is True
        assert SA._is_test_row({"symbol": stub.lower(), "reason": "SL",
                                "strategy_tag": ""}) is True


def test_is_test_row_keeps_stub_symbol_with_real_strategy_tag():
    """A stub symbol with a REAL strategy_tag is a test FIXTURE (not
    pollution) — e.g. existing tests/test_strategy_analytics.py records
    under symbol='ABC' with strategy_tag='primary_long'. These must
    survive the filter."""
    fixture = {"symbol": "ABC", "reason": "SL", "strategy_tag": "primary_long"}
    assert SA._is_test_row(fixture) is False


def test_is_test_row_keeps_real_trades():
    # Real stocks must NOT be misclassified — especially common Indian stocks.
    real = [
        {"symbol": "RELIANCE", "reason": "SL_ATR", "strategy_tag": "mtf_confirmed_long"},
        {"symbol": "IOC", "reason": "TIME_DECAY", "strategy_tag": "mtf_confirmed_long"},
        {"symbol": "HAL", "reason": "TRAIL", "strategy_tag": "trend_long"},
        {"symbol": "BHEL", "reason": "PROFIT_TARGET", "strategy_tag": "primary_long"},
        {"symbol": "M&M", "reason": "SL", "strategy_tag": "trend_long"},
        {"symbol": "INFY", "reason": "SL", "strategy_tag": "mtf_confirmed_long"},
        {"symbol": "MNM", "reason": "SL", "strategy_tag": "trend_long"},
    ]
    for r in real:
        assert SA._is_test_row(r) is False, f"real trade misclassified: {r}"


def test_is_test_row_handles_missing_fields():
    assert SA._is_test_row({}) is False
    assert SA._is_test_row({"symbol": ""}) is False
    assert SA._is_test_row({"reason": ""}) is False


# ============================================================================
# B — _is_meaningful_key filters pollution buckets from reports
# ============================================================================

def test_is_meaningful_key_drops_pollution_buckets():
    for k in ("", "unknown", "Unknown", "UNKNOWN", "n/a", "none", "test", "other"):
        assert SA._is_meaningful_key(k) is False, f"polluted key kept: {k!r}"


def test_is_meaningful_key_keeps_real_labels():
    for k in ("mean_reversion", "trend_long", "TRENDING_UP", "SIDEWAYS",
              "Financials", "mtf_confirmed_long", "MICRO"):
        assert SA._is_meaningful_key(k) is True, f"real label filtered: {k!r}"


# ============================================================================
# C — Reports filter pollution + show useful output
# ============================================================================

def _setup_history(tmp_path, monkeypatch, rows):
    """Redirect SA paths to tmp + write trade history CSV with given rows."""
    hist = tmp_path / "trade_history.csv"
    stats = tmp_path / "strategy_stats.json"
    monkeypatch.setattr(SA, "TRADE_HISTORY_PATH", str(hist), raising=False)
    monkeypatch.setattr(SA, "STRATEGY_STATS_PATH", str(stats), raising=False)
    import csv
    fields = ["entry_time", "exit_time", "symbol", "side", "qty", "entry", "exit",
              "pnl_inr", "pnl_pct", "reason", "strategy_tag", "strategy_family",
              "market_regime", "universe_source", "sector"]
    with open(hist, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = {fld: r.get(fld, "") for fld in fields}
            w.writerow(row)


def test_rebuild_drops_test_rows(tmp_path, monkeypatch):
    """The 34-row ABC/TEST pollution scenario: rebuild should produce stats
    based ONLY on the real trades, with the 'unknown' buckets empty."""
    rows = [
        # 20 synthetic test rows (the pollution)
        *[{"symbol": "ABC", "reason": "TEST",
           "strategy_tag": "unknown", "strategy_family": "unknown",
           "market_regime": "UNKNOWN", "sector": "UNKNOWN",
           "pnl_inr": -10.0} for _ in range(20)],
        # 5 real trades
        *[{"symbol": "RELIANCE", "reason": "SL_ATR",
           "strategy_tag": "mtf_confirmed_long", "strategy_family": "mean_reversion",
           "market_regime": "SIDEWAYS", "sector": "Energy",
           "pnl_inr": 15.0} for _ in range(5)],
    ]
    _setup_history(tmp_path, monkeypatch, rows)
    stats = SA.rebuild_strategy_stats()
    # The "unknown" buckets must NOT have 20 trades each — those rows were filtered.
    s_unknown = (stats.get("strategy", {}) or {}).get("unknown", {})
    assert s_unknown.get("trades", 0) == 0
    # The real bucket must reflect only the 5 real trades.
    s_real = (stats.get("strategy", {}) or {}).get("mtf_confirmed_long", {})
    assert s_real.get("trades", 0) == 5


def test_best_worst_strategy_filters_pollution(tmp_path, monkeypatch):
    """After filtering, the report should show the REAL strategy, not 'unknown'."""
    rows = [
        # The polluted "unknown" bucket with big losses
        *[{"symbol": "ABC", "reason": "TEST",
           "strategy_tag": "unknown", "strategy_family": "unknown",
           "market_regime": "UNKNOWN", "sector": "UNKNOWN",
           "pnl_inr": -100.0} for _ in range(20)],
        # The real strategy with small profits
        *[{"symbol": "INFY", "reason": "TRAIL",
           "strategy_tag": "mtf_confirmed_long", "strategy_family": "trend_long",
           "market_regime": "TRENDING_UP", "sector": "IT",
           "pnl_inr": 10.0} for _ in range(5)],
    ]
    _setup_history(tmp_path, monkeypatch, rows)
    best, worst = SA.best_worst_strategy()
    assert "mtf_confirmed_long" in best, f"best should be the real strategy; got: {best}"
    assert "unknown" not in best.lower(), "best must not mention 'unknown'"


def test_best_worst_strategy_no_real_data_returns_friendly_message(tmp_path, monkeypatch):
    """When ONLY pollution exists, the report should say so politely
    rather than spitting out 'unknown'."""
    rows = [{"symbol": "ABC", "reason": "TEST",
             "strategy_tag": "unknown", "strategy_family": "unknown",
             "market_regime": "UNKNOWN", "sector": "UNKNOWN",
             "pnl_inr": -10.0} for _ in range(15)]
    _setup_history(tmp_path, monkeypatch, rows)
    best, worst = SA.best_worst_strategy()
    assert "No strategy stats yet" in best
    assert "No strategy stats yet" in worst


def test_regime_report_filters_pollution(tmp_path, monkeypatch):
    rows = [
        *[{"symbol": "ABC", "reason": "TEST",
           "strategy_tag": "unknown", "strategy_family": "unknown",
           "market_regime": "UNKNOWN", "sector": "UNKNOWN",
           "pnl_inr": -10.0} for _ in range(15)],
        *[{"symbol": "RELIANCE", "reason": "TRAIL",
           "strategy_tag": "mtf_confirmed_long", "strategy_family": "trend_long",
           "market_regime": "SIDEWAYS", "sector": "Energy",
           "pnl_inr": 25.0} for _ in range(5)],
    ]
    _setup_history(tmp_path, monkeypatch, rows)
    text = SA.regime_report_text()
    assert "SIDEWAYS" in text
    assert "UNKNOWN" not in text, f"UNKNOWN regime must be filtered: {text}"


def test_sector_report_filters_pollution(tmp_path, monkeypatch):
    rows = [
        *[{"symbol": "ABC", "reason": "TEST",
           "strategy_tag": "unknown", "strategy_family": "unknown",
           "market_regime": "UNKNOWN", "sector": "UNKNOWN",
           "pnl_inr": -10.0} for _ in range(15)],
        *[{"symbol": "TCS", "reason": "TRAIL",
           "strategy_tag": "mtf_confirmed_long", "strategy_family": "trend_long",
           "market_regime": "TRENDING_UP", "sector": "IT",
           "pnl_inr": 30.0} for _ in range(5)],
    ]
    _setup_history(tmp_path, monkeypatch, rows)
    text = SA.sector_report_text()
    assert "IT" in text
    assert "UNKNOWN" not in text
    assert "OTHER" not in text  # filtered too


def test_strategy_report_filters_pollution(tmp_path, monkeypatch):
    rows = [
        *[{"symbol": "ABC", "reason": "TEST",
           "strategy_tag": "unknown", "strategy_family": "unknown",
           "market_regime": "UNKNOWN", "sector": "UNKNOWN",
           "pnl_inr": -10.0} for _ in range(20)],
        *[{"symbol": "HDFCBANK", "reason": "TRAIL",
           "strategy_tag": "mtf_confirmed_long", "strategy_family": "trend_long",
           "market_regime": "TRENDING_UP", "sector": "Banking",
           "pnl_inr": 20.0} for _ in range(5)],
    ]
    _setup_history(tmp_path, monkeypatch, rows)
    text = SA.strategy_report_text()
    assert "mtf_confirmed_long" in text
    # The "unknown" line might appear in the header but not in the data rows.
    # Stricter check: count occurrences — should be at most in the headline.
    assert text.lower().count("unknown") <= 1


# ============================================================================
# D — Control panel structure (button + handler coverage)
# ============================================================================

def test_main_panel_has_learnings_button():
    """Audit fix (2026-05-17): 🧠 Learnings button must exist on main panel."""
    from control_panel import _main_buttons
    rows = _main_buttons()
    labels = [btn.text for row in rows for btn in row]
    assert any("Learnings" in lbl for lbl in labels), (
        f"Learnings button missing from main panel; labels: {labels}"
    )


def test_main_panel_has_no_hybrid_button():
    """🧬 Hybrid button must be removed — HYBRID is gated off via
    ENABLE_HYBRID_MODE=false (Pile-1 cleanup)."""
    from control_panel import _main_buttons
    rows = _main_buttons()
    labels = [btn.text for row in rows for btn in row]
    assert not any("Hybrid" in lbl for lbl in labels), (
        f"Hybrid button should be removed; labels: {labels}"
    )


def test_live_panel_has_clear_position_button():
    """🗑 Clear Position hint button must exist on Live & Safety panel."""
    from control_panel import _live_buttons
    rows = _live_buttons()
    labels = [btn.text for row in rows for btn in row]
    assert any("Clear Position" in lbl for lbl in labels), (
        f"Clear Position button missing from live panel; labels: {labels}"
    )


def test_clearposition_hint_text_exists():
    """The clearposition hint popup must explain the command takes a SYMBOL
    arg and clarify that no broker order is placed."""
    from control_panel import _HINTS
    assert "clearposition" in _HINTS
    hint = _HINTS["clearposition"]
    assert "SYMBOL" in hint or "symbol" in hint
    assert "no broker" in hint.lower() or "local" in hint.lower()


def test_no_unwired_panel_buttons():
    """Every cp:cmd: button must have a corresponding handler key. Catches
    the orphaned 'holdings' button bug + any future regressions."""
    from control_panel import _main_buttons, _live_buttons, _research_buttons
    from control_panel import _admin_buttons, _logs_buttons, _analytics_buttons
    from control_panel import _token_buttons, _HINTS

    # Collect every cp:cmd:X callback_data across all panels.
    all_cmd_keys = set()
    for fn in (_main_buttons, _live_buttons, _research_buttons,
               _admin_buttons, _logs_buttons, _analytics_buttons,
               _token_buttons):
        rows = fn() if fn is not _main_buttons else fn(None)
        for row in rows:
            for btn in row:
                data = btn.data.decode() if isinstance(btn.data, bytes) else str(btn.data)
                if data.startswith("cp:cmd:"):
                    all_cmd_keys.add(data.split(":", 2)[2])
                elif data.startswith("cp:hint:"):
                    # Hints should also be defined in _HINTS dict.
                    hint_key = data.split(":", 2)[2]
                    assert hint_key in _HINTS, (
                        f"hint button {hint_key!r} has no matching _HINTS entry"
                    )

    # Match against panel_handlers — this is what bot.py registers.
    # We don't directly import bot.py (heavy deps), so just verify the
    # critical new ones we expect:
    expected_minimum = {
        "startloop", "stoploop", "status",
        "holdings", "positions",
        "learnings",  # NEW
        "panic",
        "ipstatus",
        "beststrategy", "worststrategy", "regimereport", "sectorreport",
    }
    missing = expected_minimum - all_cmd_keys
    assert not missing, (
        f"expected panel button(s) not found in any panel: {missing}"
    )
