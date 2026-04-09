import os
import sys

sys.path.insert(0, os.getcwd())

import trading_cycle as tc


def test_low_expectancy_full_aligned_mtf_long_gets_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="FULL",
        side="LONG",
        regime="TRENDING_UP",
        trend_direction="UP",
    )
    assert q == 1


def test_low_expectancy_full_aligned_mtf_short_gets_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_short",
        tier="FULL",
        side="SELL",
        regime="TRENDING_DOWN",
        trend_direction="DOWN",
    )
    assert q == 1


def test_negative_recent_still_hard_blocks(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "negative_recent"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="FULL",
        side="LONG",
        regime="TRENDING_UP",
        trend_direction="UP",
    )
    assert q == 0


def test_micro_does_not_get_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="MICRO",
        side="LONG",
        regime="TRENDING_UP",
        trend_direction="UP",
    )
    assert q == 0


def test_unaligned_regime_does_not_get_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="FULL",
        side="LONG",
        regime="WEAK",
        trend_direction="DOWN",
    )
    assert q == 0


def test_non_mtf_strategy_does_not_get_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "primary_long",
        tier="FULL",
        side="LONG",
        regime="TRENDING_UP",
        trend_direction="UP",
    )
    assert q == 0


def test_reduced_sideways_flat_mtf_long_gets_floor_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="REDUCED",
        side="BUY",
        regime="SIDEWAYS",
        trend_direction="FLAT",
    )
    assert q == 1


def test_reduced_sideways_flat_wrong_side_no_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="REDUCED",
        side="SHORT",
        regime="SIDEWAYS",
        trend_direction="FLAT",
    )
    assert q == 0


def test_reduced_sideways_flat_wrong_regime_no_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="REDUCED",
        side="LONG",
        regime="TRENDING",
        trend_direction="FLAT",
    )
    assert q == 0


def test_reduced_sideways_flat_wrong_trend_no_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_long",
        tier="REDUCED",
        side="LONG",
        regime="SIDEWAYS",
        trend_direction="UP",
    )
    assert q == 0


def test_reduced_sideways_flat_wrong_strategy_no_override(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (0.0, "low_expectancy"))
    q = tc._apply_strategy_allocation(
        1,
        "mtf_confirmed_short",
        tier="REDUCED",
        side="LONG",
        regime="SIDEWAYS",
        trend_direction="FLAT",
    )
    assert q == 0


def test_insufficient_history_behavior_unchanged(monkeypatch):
    monkeypatch.setattr(tc.SA, "get_strategy_multiplier", lambda _tag, _cfg: (1.0, "insufficient_history"))
    q = tc._apply_strategy_allocation(
        2,
        "mtf_confirmed_long",
        tier="FULL",
        side="LONG",
        regime="TRENDING_UP",
        trend_direction="UP",
    )
    assert q == 2
