"""Adaptive router (audit feature 2026-05-17): the bot learns from its
own trades and disables losing (family, regime) and (family, hour_bucket)
combos with a probe-phase recovery path.

Test plan:
  Layer 1 — Family/regime:
    - Disable threshold (WR < 35% over >=10 trades) fires correctly.
    - Insufficient data (<10 trades) does NOT trigger disable.
    - Above-threshold WR keeps combo active.
    - Suspension expires after FAMILY_SUSPEND_DAYS → enters probe phase.
    - Probe phase returns 0.5x size multiplier; suspension blocks entry.
    - probe counter decrements on each exit; clears entry on hit-zero.
    - MAX_DISABLED_FAMILIES safety floor enforced.
    - USE_ADAPTIVE_ROUTER=false → always allow, never reduce size.
    - Failed/missing trade_history.csv → fails open (no crash, allows entry).

  Layer 2 — Hour-bucket:
    - Hour-bucket mapping correct across OPEN/MID_MORN/AFTERNOON/CLOSE.
    - Block fires below threshold.
    - MIN_OPEN_BUCKETS_PER_FAMILY safety floor enforced.

  /learnings summary contains expected sections.
"""
import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.getcwd())

import config as CFG
import adaptive_router as AR

IST = ZoneInfo("Asia/Kolkata")


# ----------------------------------------------------------------------------
# Helpers — fake trade_history.csv + isolated state file per test
# ----------------------------------------------------------------------------

@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Redirect adaptive_router's state + history paths to temp files."""
    state_path = tmp_path / "adaptive_router_state.json"
    hist_path = tmp_path / "trade_history.csv"
    monkeypatch.setattr(AR, "STATE_PATH", str(state_path), raising=False)
    monkeypatch.setattr(AR, "TRADE_HISTORY_PATH", str(hist_path), raising=False)
    monkeypatch.setattr(CFG, "USE_ADAPTIVE_ROUTER", True, raising=False)
    return state_path, hist_path


def _write_trade_history(path: str, trades: list[dict]):
    """trades = [{family, regime, pnl_inr, entry_time}, ...]"""
    fields = ["entry_time", "exit_time", "symbol", "side", "qty", "entry", "exit",
              "pnl_inr", "pnl_pct", "reason", "strategy_tag", "strategy_family",
              "market_regime", "universe_source", "sector"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            row = {
                "entry_time": t.get("entry_time", "2026-05-15T11:00:00+05:30"),
                "exit_time": "",
                "symbol": "TEST",
                "side": "BUY",
                "qty": 1,
                "entry": 100.0,
                "exit": 100.0,
                "pnl_inr": t["pnl_inr"],
                "pnl_pct": 0.0,
                "reason": "TEST",
                "strategy_tag": "test",
                "strategy_family": t["family"],
                "market_regime": t["regime"],
                "universe_source": "primary",
                "sector": "X",
            }
            w.writerow(row)


def _losing_trades(n: int, family="mean_reversion", regime="SIDEWAYS"):
    """N losing trades — all -₹10 each."""
    return [{"family": family, "regime": regime, "pnl_inr": -10.0,
             "entry_time": "2026-05-15T11:00:00+05:30"} for _ in range(n)]


def _winning_trades(n: int, family="mean_reversion", regime="SIDEWAYS"):
    return [{"family": family, "regime": regime, "pnl_inr": 10.0,
             "entry_time": "2026-05-15T11:00:00+05:30"} for _ in range(n)]


# ============================================================================
# Layer 1 — Family/regime suspension
# ============================================================================

def test_family_suspended_when_wr_below_threshold(isolated_state):
    state_path, hist_path = isolated_state
    # 15 trades: 3 wins, 12 losses → WR = 20% < 35%
    trades = _winning_trades(3) + _losing_trades(12)
    _write_trade_history(hist_path, trades)

    state = AR.refresh_suspensions()
    assert "mean_reversion:SIDEWAYS" in state["family_suspensions"]
    rec = state["family_suspensions"]["mean_reversion:SIDEWAYS"]
    assert rec.get("until") is not None
    assert "20.0%" in rec.get("reason", "") or "WR=20" in rec.get("reason", "") or "win_rate=20" in rec.get("reason", "")


def test_family_NOT_suspended_with_insufficient_trades(isolated_state):
    _, hist_path = isolated_state
    # 9 trades — below FAMILY_DISABLE_MIN_N=10. Should not suspend.
    trades = _losing_trades(9)
    _write_trade_history(hist_path, trades)
    state = AR.refresh_suspensions()
    assert "mean_reversion:SIDEWAYS" not in state["family_suspensions"]


def test_family_NOT_suspended_when_wr_above_threshold(isolated_state):
    _, hist_path = isolated_state
    # 15 trades: 10 wins, 5 losses → WR = 66% well above 35%
    trades = _winning_trades(10) + _losing_trades(5)
    _write_trade_history(hist_path, trades)
    state = AR.refresh_suspensions()
    assert "mean_reversion:SIDEWAYS" not in state["family_suspensions"]


def test_suspension_blocks_entry(isolated_state):
    """is_entry_allowed must return (False, reason) for a suspended combo."""
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _losing_trades(15))
    AR.refresh_suspensions()
    allowed, reason = AR.is_entry_allowed("mean_reversion", "SIDEWAYS")
    assert allowed is False
    assert "adaptive_family_suspended" in reason


def test_size_multiplier_zero_when_suspended(isolated_state):
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _losing_trades(15))
    AR.refresh_suspensions()
    mult = AR.get_entry_size_multiplier("mean_reversion", "SIDEWAYS")
    assert mult == 0.0


def test_suspension_expiry_enters_probe_phase(isolated_state, monkeypatch):
    """After FAMILY_SUSPEND_DAYS, suspension lifts and a probe phase begins."""
    state_path, hist_path = isolated_state
    _write_trade_history(hist_path, _losing_trades(15))
    AR.refresh_suspensions()

    # Force-expire by rewriting the `until` field to the past
    state = json.loads(open(state_path).read())
    past = datetime.now(IST) - timedelta(days=1)
    state["family_suspensions"]["mean_reversion:SIDEWAYS"]["until"] = past.isoformat()
    with open(state_path, "w") as f:
        json.dump(state, f)

    # Next refresh should move it into probe phase.
    state = AR.refresh_suspensions(now=datetime.now(IST))
    rec = state["family_suspensions"].get("mean_reversion:SIDEWAYS") or {}
    assert rec.get("until") is None, "until should be cleared after probe activation"
    assert int(rec.get("probe_trades_remaining", 0)) > 0


def test_probe_phase_returns_reduced_size_multiplier(isolated_state, monkeypatch):
    """Probe phase: allowed=True, multiplier = FAMILY_REENTRY_PROBE_SIZE (0.5)."""
    state_path, _ = isolated_state
    # Manually inject a probe-phase entry.
    with open(state_path, "w") as f:
        json.dump({
            "family_suspensions": {
                "trend_long:TRENDING_UP": {
                    "until": None,
                    "probe_trades_remaining": 3,
                    "reason": "test probe",
                }
            },
            "bucket_suspensions": {},
        }, f)

    allowed, reason = AR.is_entry_allowed("trend_long", "TRENDING_UP")
    assert allowed is True
    assert reason.startswith("probe_phase")
    mult = AR.get_entry_size_multiplier("trend_long", "TRENDING_UP")
    assert mult == 0.5  # default FAMILY_REENTRY_PROBE_SIZE


def test_probe_counter_decrements_on_outcome(isolated_state):
    state_path, _ = isolated_state
    with open(state_path, "w") as f:
        json.dump({
            "family_suspensions": {
                "trend_long:TRENDING_UP": {
                    "until": None,
                    "probe_trades_remaining": 3,
                    "reason": "test",
                }
            },
            "bucket_suspensions": {},
        }, f)

    AR.record_outcome("trend_long", "TRENDING_UP", pnl_inr=15.0)
    s = json.loads(open(state_path).read())
    assert s["family_suspensions"]["trend_long:TRENDING_UP"]["probe_trades_remaining"] == 2


def test_probe_complete_clears_entry(isolated_state):
    """Last probe trade → entry deleted entirely → next entry at full size."""
    state_path, _ = isolated_state
    with open(state_path, "w") as f:
        json.dump({
            "family_suspensions": {
                "trend_long:TRENDING_UP": {
                    "until": None,
                    "probe_trades_remaining": 1,
                    "reason": "test",
                }
            },
            "bucket_suspensions": {},
        }, f)

    AR.record_outcome("trend_long", "TRENDING_UP", pnl_inr=20.0)
    s = json.loads(open(state_path).read())
    assert "trend_long:TRENDING_UP" not in s["family_suspensions"]
    # Subsequent entry: full size, allowed.
    mult = AR.get_entry_size_multiplier("trend_long", "TRENDING_UP")
    assert mult == 1.0


def test_max_disabled_families_safety_floor(isolated_state, monkeypatch):
    """Don't suspend more than MAX_DISABLED_FAMILIES at once."""
    monkeypatch.setattr(CFG, "MAX_DISABLED_FAMILIES", 1, raising=False)
    _, hist_path = isolated_state
    # Two losing combos; only ONE should suspend
    trades = (_losing_trades(15, family="mean_reversion", regime="SIDEWAYS")
              + _losing_trades(15, family="trend_long", regime="TRENDING_UP"))
    _write_trade_history(hist_path, trades)
    state = AR.refresh_suspensions()
    suspended_count = sum(
        1 for v in state["family_suspensions"].values()
        if v and v.get("until")
    )
    assert suspended_count == 1, (
        f"safety floor must cap suspensions at MAX_DISABLED_FAMILIES=1; "
        f"got {suspended_count}"
    )


def test_router_disabled_via_config(isolated_state, monkeypatch):
    """USE_ADAPTIVE_ROUTER=false → always allow, even with losing history."""
    monkeypatch.setattr(CFG, "USE_ADAPTIVE_ROUTER", False, raising=False)
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _losing_trades(15))
    # Even after refresh, queries should bypass the router entirely.
    AR.refresh_suspensions()
    allowed, _ = AR.is_entry_allowed("mean_reversion", "SIDEWAYS")
    assert allowed is True
    assert AR.get_entry_size_multiplier("mean_reversion", "SIDEWAYS") == 1.0


def test_missing_history_file_fails_open(isolated_state):
    """No trade_history.csv yet → allow everything (don't block at boot)."""
    state_path, hist_path = isolated_state
    # Don't write history
    assert not os.path.exists(hist_path)
    state = AR.refresh_suspensions()
    assert state["family_suspensions"] == {}
    assert AR.is_entry_allowed("trend_long", "TRENDING_UP") == (True, "")


# ============================================================================
# Layer 2 — Hour-bucket
# ============================================================================

@pytest.mark.parametrize("hour,minute,expected", [
    (9, 15, "OPEN"),
    (9, 30, "OPEN"),
    (10, 29, "OPEN"),
    (10, 30, "MID_MORN"),
    (11, 0, "MID_MORN"),
    (11, 59, "MID_MORN"),
    (12, 0, "AFTERNOON"),
    (13, 0, "AFTERNOON"),
    (13, 59, "AFTERNOON"),
    (14, 0, "CLOSE"),
    (15, 29, "CLOSE"),
    (15, 30, None),   # boundary: market close excluded (half-open interval)
    (8, 0, None),     # pre-market
    (16, 0, None),    # post-market
])
def test_hour_bucket_mapping(hour, minute, expected):
    dt = datetime.now(IST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    assert AR._hour_bucket(dt.isoformat()) == expected


def _mixed_trades_for_bucket_test():
    """Build trades that suspend BUCKET (MID_MORN: 12 losses) but NOT FAMILY
    (overall WR > 35%). Need at least 10 trades in the bucket for it to
    qualify, and need overall family WR > family threshold.

    Mix: 12 losing trades in MID_MORN (11:00) + 20 winning in AFTERNOON (13:00)
    → family last 30 = ~20W/10L = 67% WR (no family suspend)
    → MID_MORN bucket last 20 = 12 losses (0% WR) (suspend)
    """
    losing_mm = [{"family": "trend_long", "regime": "TRENDING_UP",
                  "pnl_inr": -10.0,
                  "entry_time": "2026-05-15T11:00:00+05:30"} for _ in range(12)]
    winning_aft = [{"family": "trend_long", "regime": "TRENDING_UP",
                    "pnl_inr": +20.0,
                    "entry_time": "2026-05-15T13:00:00+05:30"} for _ in range(20)]
    # Interleave so the last 30 don't all happen to be the wins
    return losing_mm + winning_aft


def test_bucket_blocked_when_wr_below_threshold(isolated_state):
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _mixed_trades_for_bucket_test())
    state = AR.refresh_suspensions()
    # Bucket should suspend; family should NOT (mixed WR is above threshold).
    assert "trend_long:MID_MORN" in state["bucket_suspensions"], (
        f"MID_MORN should be blocked; got: {list(state['bucket_suspensions'].keys())}"
    )
    assert "trend_long:TRENDING_UP" not in state["family_suspensions"], (
        "Family should NOT be suspended (overall WR > 35%)"
    )


def test_bucket_block_returns_false_when_query_in_blocked_hour(isolated_state):
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _mixed_trades_for_bucket_test())
    AR.refresh_suspensions()

    # Query at 11:00 = MID_MORN bucket (blocked)
    query_time = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
    allowed, reason = AR.is_entry_allowed("trend_long", "TRENDING_UP", now=query_time)
    assert allowed is False
    assert "adaptive_bucket_blocked" in reason


def test_bucket_block_NOT_returned_outside_blocked_hour(isolated_state):
    """Block applies to MID_MORN only — entries in AFTERNOON still allowed."""
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _mixed_trades_for_bucket_test())
    AR.refresh_suspensions()

    query_time = datetime.now(IST).replace(hour=13, minute=0, second=0, microsecond=0)
    allowed, _ = AR.is_entry_allowed("trend_long", "TRENDING_UP", now=query_time)
    assert allowed is True


def test_min_open_buckets_safety_floor(isolated_state, monkeypatch):
    """Must leave at least MIN_OPEN_BUCKETS_PER_FAMILY hour buckets open."""
    monkeypatch.setattr(CFG, "MIN_OPEN_BUCKETS_PER_FAMILY", 2, raising=False)
    _, hist_path = isolated_state
    # Losing in 3 buckets — but safety floor must keep at least 2 open
    bucket_times = ["09:30:00", "11:00:00", "13:00:00", "14:30:00"]
    trades = []
    for bt in bucket_times:
        trades.extend([{"family": "trend_long", "regime": "TRENDING_UP",
                        "pnl_inr": -10.0,
                        "entry_time": f"2026-05-15T{bt}+05:30"}
                       for _ in range(12)])
    _write_trade_history(hist_path, trades)
    state = AR.refresh_suspensions()
    blocked = [k for k, v in state["bucket_suspensions"].items()
               if k.startswith("trend_long:") and v and v.get("until")]
    # With 4 buckets total and MIN_OPEN=2, at most 2 can be blocked.
    assert len(blocked) <= 2, f"safety floor must keep ≥2 buckets open; blocked={blocked}"


# ============================================================================
# /learnings summary
# ============================================================================

def test_learnings_summary_includes_active_suspensions(isolated_state):
    _, hist_path = isolated_state
    _write_trade_history(hist_path, _losing_trades(15))
    summary = AR.get_learnings_summary()
    assert "Layer 1" in summary
    assert "mean_reversion" in summary
    assert "Recent" in summary  # the per-combo WR section


def test_learnings_summary_clean_state(isolated_state):
    """With no trades, summary still works (no crashes, no suspensions)."""
    summary = AR.get_learnings_summary()
    assert "nothing suspended" in summary
    assert "nothing blocked" in summary
