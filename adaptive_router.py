"""Adaptive routing — the bot learns from its own trades (paper or live).

Layered learning system (audit 2026-05-17):

  Layer 1: Regime-gated family disable
    For each (strategy_family, regime) combo, look at last N trades.
    If win_rate < FAMILY_DISABLE_MIN_WR AND trades >= FAMILY_DISABLE_MIN_N
    → suspend that combo for SUSPEND_DAYS. After suspension lifts,
    re-test at 50% size for SUSPEND_REENTRY_PROBE trades before full size.

  Layer 2: Time-of-day bucket learning
    For each (strategy_family, hour_bucket) combo, look at last M trades.
    Hour buckets: OPEN (09:15-10:30), MID_MORN (10:30-12:00),
                  AFTERNOON (12:00-14:00), CLOSE (14:00-15:30).
    If win_rate < BUCKET_DISABLE_MIN_WR AND trades >= BUCKET_DISABLE_MIN_N
    → block that combo for BUCKET_SUSPEND_DAYS, then re-test.

State persistence:
  data/adaptive_router_state.json — holds suspension/block records with
  the date each was created and a one-line reason. Survives restarts.

Integration:
  - trading_cycle entry routing calls is_entry_allowed(family, regime, now)
    BEFORE generating signals for a family. Returns (allowed, reason).
  - On every trade exit, record_outcome(...) updates internal counters
    used to drive next-day decisions (cheap; full recompute is fast).
  - The /learnings Telegram command surfaces what's currently suspended.

Safety floors:
  - Never disable >= MAX_DISABLED_FAMILIES at once (default 2 of N).
  - Never block all 4 hour buckets for any single family.
  - Re-test probes always run at 50% size, never 0% — so the bot never
    forgets a strategy that recovers.

Empirical-only by design:
  This learns "what HAS worked" not "what WILL work." Markets shift
  regimes, so the recovery / re-test paths matter as much as the
  disable paths. Daily update cadence (not per-tick) to avoid
  overfitting to morning noise.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import config as CFG
from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
TRADE_HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")
STATE_PATH = os.path.join(DATA_DIR, "adaptive_router_state.json")


# ----------------------------------------------------------------------------
# Defaults (overridable via CFG)
# ----------------------------------------------------------------------------

# Layer 1
FAMILY_DISABLE_LOOKBACK_TRADES = 30   # rolling window
FAMILY_DISABLE_MIN_N = 10             # need this many trades before judging
FAMILY_DISABLE_MIN_WR = 35.0          # below this win-rate %, suspend
FAMILY_SUSPEND_DAYS = 5               # days to keep suspended
FAMILY_REENTRY_PROBE_SIZE = 0.50      # re-test at 50% size
FAMILY_REENTRY_PROBE_TRADES = 5       # number of probe trades before full size

# Layer 2
BUCKET_DISABLE_LOOKBACK_TRADES = 20
BUCKET_DISABLE_MIN_N = 10
BUCKET_DISABLE_MIN_WR = 30.0
BUCKET_SUSPEND_DAYS = 3

# Hour-bucket boundaries (start_hour, start_min, end_hour, end_min) in IST
HOUR_BUCKETS = {
    "OPEN":     (9, 15, 10, 30),
    "MID_MORN": (10, 30, 12, 0),
    "AFTERNOON":(12, 0, 14, 0),
    "CLOSE":    (14, 0, 15, 30),
}

# Safety floors
MAX_DISABLED_FAMILIES = 2     # of the ~6 active families
MIN_OPEN_BUCKETS_PER_FAMILY = 1  # at least 1 hour bucket must be open per family


def _cfg(name: str, default):
    """Read CFG override or fall back to default constant in this module."""
    try:
        v = getattr(CFG, name, None)
        if v is None:
            return default
        return type(default)(v) if not isinstance(default, bool) else bool(v)
    except Exception:
        return default


# ----------------------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------------------
#
# Module-level mtime cache (audit fix 2026-05-17): `is_entry_allowed` is
# called per-family per-tick (3-6× per 20s loop = ~10k calls/day). Without
# this cache each call re-opened and re-parsed the JSON state file. Now we
# only re-read when the file's mtime (or path — for tests) changes.
_STATE_CACHE: Optional[dict] = None
_STATE_CACHE_MTIME: float = 0.0
_STATE_CACHE_PATH: str = ""


def _empty_state() -> dict:
    return {"family_suspensions": {}, "bucket_suspensions": {}, "updated_at": None}


def _load_state() -> dict:
    """{
        'family_suspensions': {'<family>:<regime>': {
              'suspended_at': '2026-05-17T18:00:00+05:30',
              'until': '2026-05-22T18:00:00+05:30',
              'reason': 'win_rate=22% over 15 trades',
              'probe_trades_remaining': 0,
        }},
        'bucket_suspensions': {'<family>:<bucket>': {... same shape ...}},
        'updated_at': '...'
    }"""
    global _STATE_CACHE, _STATE_CACHE_MTIME, _STATE_CACHE_PATH
    cur_path = STATE_PATH
    try:
        mtime = os.path.getmtime(cur_path)
    except OSError:
        # File doesn't exist. If the cached value is for the SAME path, return
        # it; otherwise return a fresh empty state (path changed between
        # tests, or this is first ever call).
        if _STATE_CACHE is not None and _STATE_CACHE_PATH == cur_path:
            return _STATE_CACHE
        return _empty_state()
    if (
        _STATE_CACHE is not None
        and _STATE_CACHE_PATH == cur_path
        and mtime == _STATE_CACHE_MTIME
    ):
        return _STATE_CACHE
    try:
        with open(cur_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("family_suspensions", {})
        data.setdefault("bucket_suspensions", {})
        _STATE_CACHE = data
        _STATE_CACHE_MTIME = mtime
        _STATE_CACHE_PATH = cur_path
        return data
    except Exception as e:
        append_log("WARN", "ADAPTIVE", f"failed to load adaptive_router_state: {e}")
        return _empty_state()


def _save_state(state: dict) -> None:
    global _STATE_CACHE, _STATE_CACHE_MTIME, _STATE_CACHE_PATH
    cur_path = STATE_PATH
    state["updated_at"] = datetime.now(IST).isoformat(timespec="seconds")
    tmp = cur_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, cur_path)
        # Refresh the cache in place so subsequent _load_state() calls don't
        # re-read the file we just wrote. mtime is bumped by os.replace.
        _STATE_CACHE = state
        _STATE_CACHE_PATH = cur_path
        try:
            _STATE_CACHE_MTIME = os.path.getmtime(cur_path)
        except OSError:
            _STATE_CACHE_MTIME = 0.0
    except Exception as e:
        append_log("WARN", "ADAPTIVE", f"failed to save adaptive_router_state: {e}")


# ----------------------------------------------------------------------------
# Trade-history loading + per-bucket stats
# ----------------------------------------------------------------------------
#
# Reuse strategy_analytics helpers instead of re-implementing (audit fix
# 2026-05-17). _safe_float and _read_csv_rows behave identically; this drops
# ~15 lines of duplicated code and keeps the CSV-reader behaviour in sync.
from strategy_analytics import _safe_float, _read_csv_rows


def _hour_bucket(entry_time_str: str) -> Optional[str]:
    """Map an ISO entry_time to a hour-bucket name. Returns None if unparseable."""
    if not entry_time_str:
        return None
    try:
        dt = datetime.fromisoformat(str(entry_time_str))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST)
    minutes = dt.hour * 60 + dt.minute
    for name, (sh, sm, eh, em) in HOUR_BUCKETS.items():
        if (sh * 60 + sm) <= minutes < (eh * 60 + em):
            return name
    return None


def _load_recent_trades(lookback: int) -> list[dict]:
    """Read the most recent `lookback` trades from trade_history.csv.
    Returns [] if file missing or unreadable. Cheap — file is small."""
    rows = _read_csv_rows(TRADE_HISTORY_PATH)
    return rows[-lookback:] if lookback > 0 else rows


def _wr_for_subset(rows: list[dict]) -> tuple[int, float]:
    """Return (trade_count, win_rate_pct) for a subset of trade rows."""
    n = len(rows)
    if n == 0:
        return 0, 0.0
    wins = sum(1 for r in rows if _safe_float(r.get("pnl_inr")) > 0)
    return n, (wins / n * 100.0)


def _group_by_key(rows: list[dict], lookback: int, key_fn) -> dict:
    """Group rows by the key returned from key_fn(row); skip rows where key
    is None/empty. Returns {key: (trade_count, win_rate_pct)} computed over
    the most recent `lookback` trades per combo."""
    buckets: dict = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if not k:
            continue
        buckets[k].append(r)
    return {k: _wr_for_subset(subset[-lookback:]) for k, subset in buckets.items()}


def compute_family_regime_stats(rows: Optional[list[dict]] = None) -> dict:
    """Returns {(family, regime): (trade_count, win_rate_pct)} from
    the most recent FAMILY_DISABLE_LOOKBACK_TRADES trades.

    `rows` lets the caller pass a pre-loaded trade list to avoid duplicate
    CSV scans (refresh_suspensions does this).
    """
    lookback = _cfg("FAMILY_DISABLE_LOOKBACK_TRADES", FAMILY_DISABLE_LOOKBACK_TRADES)
    if rows is None:
        rows = _load_recent_trades(lookback * 6)

    def _key(r):
        fam = str(r.get("strategy_family") or "").strip().lower()
        reg = str(r.get("market_regime") or "").strip().upper()
        return (fam, reg) if (fam and reg) else None

    return _group_by_key(rows, lookback, _key)


def compute_family_bucket_stats(rows: Optional[list[dict]] = None) -> dict:
    """Returns {(family, hour_bucket): (trade_count, win_rate_pct)}.

    `rows` lets the caller pass a pre-loaded trade list (see refresh_suspensions).
    """
    lookback = _cfg("BUCKET_DISABLE_LOOKBACK_TRADES", BUCKET_DISABLE_LOOKBACK_TRADES)
    if rows is None:
        rows = _load_recent_trades(lookback * 6)

    def _key(r):
        fam = str(r.get("strategy_family") or "").strip().lower()
        bucket = _hour_bucket(r.get("entry_time"))
        return (fam, bucket) if (fam and bucket) else None

    return _group_by_key(rows, lookback, _key)


# ----------------------------------------------------------------------------
# Daily refresh — call once per day (or on-demand from /learnings)
# ----------------------------------------------------------------------------

def refresh_suspensions(now: Optional[datetime] = None) -> dict:
    """Recompute the suspension set based on latest trade_history.

    Returns the updated state dict. Should be called once at start-of-day
    or on-demand via the /learnings command. Cheap (~ms).
    """
    now = now or datetime.now(IST)
    state = _load_state()

    # 1a) Lift expired FAMILY suspensions → enter probe phase (re-test at reduced size).
    for key in list(state["family_suspensions"].keys()):
        rec = state["family_suspensions"][key] or {}
        until = rec.get("until")
        if not until:
            continue
        try:
            until_dt = datetime.fromisoformat(str(until))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=IST)
            if now >= until_dt:
                if rec.get("probe_trades_remaining", 0) <= 0:
                    rec["probe_trades_remaining"] = _cfg("FAMILY_REENTRY_PROBE_TRADES", FAMILY_REENTRY_PROBE_TRADES)
                    rec["until"] = None
                    append_log("INFO", "ADAPTIVE",
                               f"suspension_lifted family key={key} → entering probe phase "
                               f"(probe_trades={rec['probe_trades_remaining']})")
        except Exception:
            pass

    # 1b) Lift expired BUCKET blocks → delete entry entirely (no probe phase
    # for hour buckets — they re-test naturally as the lookback window slides
    # forward and includes new trades from the previously-blocked hour).
    for key in list(state["bucket_suspensions"].keys()):
        rec = state["bucket_suspensions"][key] or {}
        until = rec.get("until")
        if not until:
            continue
        try:
            until_dt = datetime.fromisoformat(str(until))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=IST)
            if now >= until_dt:
                del state["bucket_suspensions"][key]
                append_log("INFO", "ADAPTIVE", f"bucket_block_lifted key={key}")
        except Exception:
            pass

    # 2) Add new suspensions where thresholds tripped.
    # Load trade history ONCE here and pass to both compute calls so we
    # don't do two full CSV scans per refresh.
    fam_lookback = _cfg("FAMILY_DISABLE_LOOKBACK_TRADES", FAMILY_DISABLE_LOOKBACK_TRADES)
    buck_lookback = _cfg("BUCKET_DISABLE_LOOKBACK_TRADES", BUCKET_DISABLE_LOOKBACK_TRADES)
    rows = _load_recent_trades(max(fam_lookback, buck_lookback) * 6)

    # Layer 1: family-regime.
    fr_stats = compute_family_regime_stats(rows=rows)
    min_n = _cfg("FAMILY_DISABLE_MIN_N", FAMILY_DISABLE_MIN_N)
    min_wr = _cfg("FAMILY_DISABLE_MIN_WR", FAMILY_DISABLE_MIN_WR)
    suspend_days = _cfg("FAMILY_SUSPEND_DAYS", FAMILY_SUSPEND_DAYS)

    new_suspensions_count = sum(
        1 for k, v in state["family_suspensions"].items()
        if v and v.get("until")  # currently in suspension (not probe)
    )

    for (fam, reg), (n, wr) in fr_stats.items():
        key = f"{fam}:{reg}"
        already = state["family_suspensions"].get(key) or {}
        # Don't re-suspend an entry already suspended (until is set) OR
        # currently in probe-phase recovery (until=None but probe counter > 0).
        # Without this guard, a combo with losing history would be re-suspended
        # on every refresh, never getting a chance to probe back to full size.
        in_probe = int(already.get("probe_trades_remaining", 0) or 0) > 0
        if n >= min_n and wr < min_wr and not already.get("until") and not in_probe:
            # Safety floor: don't exceed MAX_DISABLED_FAMILIES.
            max_disabled = _cfg("MAX_DISABLED_FAMILIES", MAX_DISABLED_FAMILIES)
            if new_suspensions_count >= max_disabled:
                append_log("INFO", "ADAPTIVE",
                           f"would_suspend family={fam} regime={reg} wr={wr:.1f}% n={n} "
                           f"but at max_disabled={max_disabled} — kept active")
                continue
            until = now + timedelta(days=suspend_days)
            state["family_suspensions"][key] = {
                "suspended_at": now.isoformat(timespec="seconds"),
                "until": until.isoformat(timespec="seconds"),
                "reason": f"win_rate={wr:.1f}% over last {n} trades",
                "probe_trades_remaining": 0,
            }
            new_suspensions_count += 1
            append_log("WARN", "ADAPTIVE",
                       f"suspended family={fam} regime={reg} wr={wr:.1f}% n={n} "
                       f"until={until.strftime('%Y-%m-%d')}")

    # Layer 2: family-bucket. Reuses the same `rows` loaded above.
    b_stats = compute_family_bucket_stats(rows=rows)
    bmin_n = _cfg("BUCKET_DISABLE_MIN_N", BUCKET_DISABLE_MIN_N)
    bmin_wr = _cfg("BUCKET_DISABLE_MIN_WR", BUCKET_DISABLE_MIN_WR)
    bsuspend_days = _cfg("BUCKET_SUSPEND_DAYS", BUCKET_SUSPEND_DAYS)

    for (fam, bucket), (n, wr) in b_stats.items():
        key = f"{fam}:{bucket}"
        already = state["bucket_suspensions"].get(key) or {}
        if n >= bmin_n and wr < bmin_wr and not already.get("until"):
            # Safety floor: must keep at least MIN_OPEN_BUCKETS_PER_FAMILY open.
            cur_blocked_for_fam = sum(
                1 for k, v in state["bucket_suspensions"].items()
                if k.startswith(f"{fam}:") and v and v.get("until")
            )
            if (len(HOUR_BUCKETS) - cur_blocked_for_fam - 1) < _cfg(
                "MIN_OPEN_BUCKETS_PER_FAMILY", MIN_OPEN_BUCKETS_PER_FAMILY
            ):
                append_log("INFO", "ADAPTIVE",
                           f"would_block_bucket family={fam} bucket={bucket} wr={wr:.1f}% "
                           f"but would leave fewer than min open — kept active")
                continue
            until = now + timedelta(days=bsuspend_days)
            state["bucket_suspensions"][key] = {
                "suspended_at": now.isoformat(timespec="seconds"),
                "until": until.isoformat(timespec="seconds"),
                "reason": f"win_rate={wr:.1f}% over last {n} trades",
                "probe_trades_remaining": 0,
            }
            append_log("WARN", "ADAPTIVE",
                       f"blocked_bucket family={fam} bucket={bucket} wr={wr:.1f}% n={n} "
                       f"until={until.strftime('%Y-%m-%d')}")

    _save_state(state)
    return state


# ----------------------------------------------------------------------------
# Public API consumed by trading_cycle entry routing
# ----------------------------------------------------------------------------

def is_entry_allowed(strategy_family: str, regime: str,
                     now: Optional[datetime] = None) -> tuple[bool, str]:
    """Returns (allowed, reason).

    - allowed=True means the entry can proceed at full size.
    - allowed=True with reason="probe_phase:X" means proceed at 50% size
      (caller checks the prefix and applies REENTRY_PROBE_SIZE).
    - allowed=False means BLOCK the entry; `reason` is a human-readable
      string suitable for log/skip records.

    Cheap — does NOT recompute stats; reads cached state file.
    """
    if not bool(_cfg("USE_ADAPTIVE_ROUTER", True)):
        return True, ""
    now = now or datetime.now(IST)
    fam = str(strategy_family or "").strip().lower()
    reg = str(regime or "").strip().upper()
    state = _load_state()

    # Layer 1 check
    f_key = f"{fam}:{reg}"
    f_rec = state.get("family_suspensions", {}).get(f_key) or {}
    if f_rec.get("until"):
        try:
            until_dt = datetime.fromisoformat(str(f_rec["until"]))
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=IST)
            if now < until_dt:
                return False, f"adaptive_family_suspended:{f_key}:{f_rec.get('reason','')}"
        except Exception:
            pass
    probe_remaining = int(f_rec.get("probe_trades_remaining", 0) or 0)
    in_probe = probe_remaining > 0 and not f_rec.get("until")

    # Layer 2 check
    bucket = _hour_bucket(now.isoformat())
    if bucket:
        b_key = f"{fam}:{bucket}"
        b_rec = state.get("bucket_suspensions", {}).get(b_key) or {}
        if b_rec.get("until"):
            try:
                until_dt = datetime.fromisoformat(str(b_rec["until"]))
                if until_dt.tzinfo is None:
                    until_dt = until_dt.replace(tzinfo=IST)
                if now < until_dt:
                    return False, f"adaptive_bucket_blocked:{b_key}:{b_rec.get('reason','')}"
            except Exception:
                pass

    if in_probe:
        return True, f"probe_phase:{f_key}:remaining={probe_remaining}"
    return True, ""


def get_entry_size_multiplier(strategy_family: str, regime: str,
                              now: Optional[datetime] = None) -> float:
    """If the (family, regime) is in re-test probe phase, return the
    reduced size multiplier. Otherwise 1.0."""
    if not bool(_cfg("USE_ADAPTIVE_ROUTER", True)):
        return 1.0
    allowed, reason = is_entry_allowed(strategy_family, regime, now=now)
    if not allowed:
        return 0.0
    if reason.startswith("probe_phase"):
        return float(_cfg("FAMILY_REENTRY_PROBE_SIZE", FAMILY_REENTRY_PROBE_SIZE))
    return 1.0


def record_outcome(strategy_family: str, regime: str, pnl_inr: float,
                   now: Optional[datetime] = None) -> None:
    """Update probe counters when a probe trade completes. The bulk of
    stats live in trade_history.csv; this only needs to track the
    probe-phase countdown."""
    if not bool(_cfg("USE_ADAPTIVE_ROUTER", True)):
        return
    fam = str(strategy_family or "").strip().lower()
    reg = str(regime or "").strip().upper()
    state = _load_state()
    f_key = f"{fam}:{reg}"
    f_rec = state.get("family_suspensions", {}).get(f_key) or {}
    if not f_rec or f_rec.get("until"):
        return  # not in probe phase
    remaining = int(f_rec.get("probe_trades_remaining", 0) or 0)
    if remaining <= 0:
        return
    remaining -= 1
    f_rec["probe_trades_remaining"] = remaining
    if remaining <= 0:
        # Probe complete → clear the entry entirely (back to full size).
        del state["family_suspensions"][f_key]
        append_log("INFO", "ADAPTIVE",
                   f"probe_complete family={fam} regime={reg} → cleared, back to full size")
    else:
        state["family_suspensions"][f_key] = f_rec
    _save_state(state)


def get_learnings_summary(now: Optional[datetime] = None) -> str:
    """Return a Telegram-friendly multi-line string of current
    suspensions/blocks + their reasons. Used by the /learnings command."""
    now = now or datetime.now(IST)
    state = refresh_suspensions(now=now)
    lines = ["📊 *Adaptive learnings*"]
    fs = state.get("family_suspensions", {}) or {}
    bs = state.get("bucket_suspensions", {}) or {}

    fam_lines = []
    for key, rec in sorted(fs.items()):
        if not rec:
            continue
        until = rec.get("until")
        probe = int(rec.get("probe_trades_remaining", 0) or 0)
        reason = rec.get("reason", "?")
        if until:
            fam_lines.append(f"  🚫 {key} → suspended until {str(until)[:10]} ({reason})")
        elif probe > 0:
            fam_lines.append(f"  🟡 {key} → probe phase, {probe} trades remaining ({reason})")
    if fam_lines:
        lines.append("*Layer 1 — Family/regime:*")
        lines.extend(fam_lines)
    else:
        lines.append("*Layer 1 — Family/regime:* nothing suspended")

    buck_lines = []
    for key, rec in sorted(bs.items()):
        if not rec or not rec.get("until"):
            continue
        until = rec.get("until")
        reason = rec.get("reason", "?")
        buck_lines.append(f"  🚫 {key} → blocked until {str(until)[:10]} ({reason})")
    if buck_lines:
        lines.append("*Layer 2 — Hour buckets:*")
        lines.extend(buck_lines)
    else:
        lines.append("*Layer 2 — Hour buckets:* nothing blocked")

    # Show some stats too — useful diagnostic.
    fr = compute_family_regime_stats()
    if fr:
        lines.append("\n*Recent (family, regime) win-rates:*")
        # Sort by win_rate ascending (worst first) so the user sees risky combos.
        items = sorted(fr.items(), key=lambda kv: (kv[1][1], -kv[1][0]))
        for (fam, reg), (n, wr) in items[:8]:
            badge = "🔴" if wr < 35 else ("🟡" if wr < 50 else "🟢")
            lines.append(f"  {badge} {fam} / {reg}: WR={wr:.1f}% (n={n})")
    return "\n".join(lines)
