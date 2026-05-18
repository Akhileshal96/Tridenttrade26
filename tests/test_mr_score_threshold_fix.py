"""Audit fix (2026-05-18): MIN_SCORE_MEAN_REVERSION lowered 4.0 -> 1.5.

Bug context:
  Day 1 of paper week (2026-05-18) produced ZERO trades despite the bot
  running for 2+ hours. Investigation found 8 ITC mean-reversion BUY
  candidates were generated but all rejected at the same gate:

    [SIG] mean_reversion best=ITC score=1.8966 below min=4.00 -> skipped

  The MR scoring formula (strategy_engine._score_mean_reversion_setup):
    score = 0.50 * max(0, 30 - rsi)
          + 0.30 * max(0, bounce_size_pct)
          + 0.20 * max(0, recovery_momentum_pct)

  For score >= 4.0 you'd need RSI < 22 (rare crash territory), or a 13%+
  BB bounce, or a giant momentum bar. None of these are normal intraday
  mean-reversion setups. The 4.0 threshold effectively disabled the
  family in normal markets — and MR is one of the bot's three primary
  long-side families.

  Trend-long uses MIN_SCORE_TREND_LONG=1.0 — meaning MR was 4x stricter
  than trend without justification.

Fix:
  Lower MIN_SCORE_MEAN_REVERSION default from 4.0 to 1.5. With today's
  observed score distribution (max 1.90, mean ~1.4), this captures the
  top ~25% of setups without flooding with weak signals.

  Today's ITC top scores: 1.0735, 1.4181, 1.7698, 1.8966.
  At threshold 1.5, the top 2 (1.77 and 1.90) would have entered.
  At threshold 4.0, none entered.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

import config as CFG


def test_mr_score_threshold_default_is_1_5_not_4_0():
    """If this fails: someone bumped the default back up. See the audit
    comment in config.py around MIN_SCORE_MEAN_REVERSION. 4.0 disabled
    the MR family in normal markets — do not revert without first
    studying the score distribution from a recent paper-trading log."""
    assert CFG.MIN_SCORE_MEAN_REVERSION == 1.5, (
        f"MIN_SCORE_MEAN_REVERSION should default to 1.5; got "
        f"{CFG.MIN_SCORE_MEAN_REVERSION}. The 4.0 value (pre-2026-05-18) "
        f"made MR effectively un-tradeable in normal markets."
    )


def test_mr_threshold_admits_realistic_oversold_bounce():
    """A REALISTIC mean-reversion setup should clear the gate.

    Setup we want to admit: RSI=26, BB bounce 0.5%, recovery momentum 0.3%.
    This is a textbook decent MR opportunity (oversold + small bounce +
    a positive last bar). It should NOT be filtered out as 'weak'.
    """
    rsi_last = 26.0
    bounce_size_pct = 0.5
    recovery_momentum_pct = 0.3

    # Replicate the scoring formula from strategy_engine._score_mean_reversion_setup
    rsi_depth = max(0.0, 30.0 - rsi_last)
    score = (
        (0.50 * rsi_depth)
        + (0.30 * max(0.0, bounce_size_pct))
        + (0.20 * max(0.0, recovery_momentum_pct))
    )
    # 0.5*4 + 0.3*0.5 + 0.2*0.3 = 2.0 + 0.15 + 0.06 = 2.21
    assert score >= CFG.MIN_SCORE_MEAN_REVERSION, (
        f"a realistic oversold bounce (RSI=26, bounce=0.5%) scored "
        f"{score:.2f} but threshold is {CFG.MIN_SCORE_MEAN_REVERSION}. "
        f"Threshold is too strict — would have rejected this entry."
    )


def test_mr_threshold_rejects_clearly_weak_setup():
    """A WEAK setup should still be filtered. Sanity check that we
    haven't gone too loose.

    Setup we want to reject: RSI=29 (barely oversold), bounce 0.1%, no
    momentum. This is a "we're almost in MR territory" setup that
    typically results in chop, not a real bounce.
    """
    rsi_last = 29.0
    bounce_size_pct = 0.1
    recovery_momentum_pct = 0.0
    rsi_depth = max(0.0, 30.0 - rsi_last)
    score = (
        (0.50 * rsi_depth)
        + (0.30 * max(0.0, bounce_size_pct))
        + (0.20 * max(0.0, recovery_momentum_pct))
    )
    # 0.5*1 + 0.3*0.1 + 0.2*0 = 0.5 + 0.03 = 0.53
    assert score < CFG.MIN_SCORE_MEAN_REVERSION, (
        f"a clearly-weak setup (RSI=29, bounce=0.1%) scored {score:.2f}; "
        f"threshold {CFG.MIN_SCORE_MEAN_REVERSION} would have admitted "
        f"it. Threshold is too loose."
    )


def test_mr_threshold_admits_todays_top_itc_signal():
    """The exact ITC signal from today's paper log (May 18) should now
    be tradeable. This is the regression case for the audit fix.

    From log: '[SIG] ITC MR BUY candidate last=308.15 rsi=26.47
              lower_bb=306.80 setup=RSI (ranked vs other candidates)'
    Score: 1.8966 (rejected at old 4.0 threshold).
    """
    rsi_last = 26.47
    bounce_size_pct = ((308.15 - 306.80) / 306.80) * 100.0  # ~0.44
    recovery_momentum_pct = 0.0  # not in the log, use 0 to be conservative
    rsi_depth = max(0.0, 30.0 - rsi_last)
    score = (
        (0.50 * rsi_depth)
        + (0.30 * max(0.0, bounce_size_pct))
        + (0.20 * max(0.0, recovery_momentum_pct))
    )
    # Approximately 0.5*3.53 + 0.3*0.44 = 1.765 + 0.132 = 1.897
    assert 1.8 < score < 2.0, (
        f"the ITC signal should score ~1.90; got {score:.4f}"
    )
    assert score >= CFG.MIN_SCORE_MEAN_REVERSION, (
        f"today's actual ITC signal (score={score:.2f}) must pass the "
        f"new threshold {CFG.MIN_SCORE_MEAN_REVERSION}. If this fails, "
        f"tomorrow's session will produce zero MR trades again."
    )


def test_mr_threshold_overridable_via_env(monkeypatch):
    """If a user genuinely wants the old 4.0 threshold back (e.g., after
    seeing terrible MR win-rates), MIN_SCORE_MEAN_REVERSION can still be
    overridden via .env. Verify the config plumbing works."""
    import importlib
    monkeypatch.setenv("MIN_SCORE_MEAN_REVERSION", "3.0")
    import config as fresh_cfg
    importlib.reload(fresh_cfg)
    try:
        assert fresh_cfg.MIN_SCORE_MEAN_REVERSION == 3.0
    finally:
        # Restore the default so subsequent tests see the production value.
        monkeypatch.delenv("MIN_SCORE_MEAN_REVERSION", raising=False)
        importlib.reload(fresh_cfg)
