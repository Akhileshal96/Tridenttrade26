#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$ROOT/backup_$TS"
mkdir -p "$BACKUP_DIR"

echo "==> Trident PATCH V2 starting..."
echo "==> Backup dir: $BACKUP_DIR"

# ---------- helper: safe append env key if missing ----------
add_env_kv() {
  local key="$1"
  local value="$2"
  local envfile="$ROOT/.env"
  touch "$envfile"
  if grep -qE "^${key}=" "$envfile"; then
    echo "   - .env has $key (keeping existing)"
  else
    echo "${key}=${value}" >> "$envfile"
    echo "   + Added .env $key"
  fi
}

# ---------- Backup current files ----------
backup_file() {
  local f="$1"
  if [ -f "$ROOT/$f" ]; then
    cp -a "$ROOT/$f" "$BACKUP_DIR/"
    echo "   + Backed up $f"
  else
    echo "   - $f not found (skipping backup)"
  fi
}

for f in bot.py trading_cycle.py night_research.py strategy_engine.py config.py env_utils.py instrument_store.py; do
  backup_file "$f"
done

mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/cache"

# ---------- Ensure .env contains required keys ----------
# Universe separation
add_env_kv "UNIVERSE_LIVE_PATH" "$ROOT/data/universe_live.txt"
add_env_kv "UNIVERSE_TRADING_PATH" "$ROOT/data/universe_trading.txt"

# Auto promote
add_env_kv "AUTO_PROMOTE_ENABLED" "true"
add_env_kv "PROMOTE_COOLDOWN_MIN" "60"
add_env_kv "PROMOTE_WINDOWS" "10:00-10:10,12:00-12:10,13:30-13:40"
add_env_kv "PROMOTE_TOP10_OVERLAP_MIN" "0.60"
add_env_kv "STABILITY_ATR_PCT_MAX" "0.35"
add_env_kv "STABILITY_SYMBOL" "NIFTYBEES"

# Night scheduler
add_env_kv "NIGHT_AUTO_ENABLED" "true"
add_env_kv "NIGHT_START" "18:30"
add_env_kv "NIGHT_INTERVAL_MIN" "90"
add_env_kv "NIGHT_END_OFFSET_MIN" "5"

# Slippage runtime default
add_env_kv "MAX_ENTRY_SLIPPAGE_PCT" "0.30"

# Multi-user roles (backward compatible)
# If you already have ADMIN_USER_ID, keep it; bot.py will treat it as OWNER if OWNER_USER_ID is missing.
add_env_kv "OWNER_USER_ID" "$(grep -E '^ADMIN_USER_ID=' "$ROOT/.env" | tail -n 1 | cut -d= -f2 | tr -d ' ' || true)"
add_env_kv "TRADER_USER_IDS" ""
add_env_kv "VIEWER_USER_IDS" ""

# Token generation support (optional). Leave blank if you don't want to store secret.
add_env_kv "KITE_API_SECRET" ""

# ---------- env_utils.py (FIXED & robust) ----------
cat > "$ROOT/env_utils.py" <<'PY'
import os

ENV_PATH = os.path.join(os.getcwd(), ".env")

def set_env_value(key: str, value: str) -> None:
    """
    Upserts KEY=VALUE into .env safely.
    """
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            lines = f.read().splitlines()

    out = []
    found = False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)

    if not found:
        out.append(f"{key}={value}")

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(out) + "\n")


def get_env_value(key: str, default: str = "") -> str:
    """
    Reads KEY from .env (not from process env). Useful for runtime admin updates.
    """
    if not os.path.exists(ENV_PATH):
        return default
    with open(ENV_PATH, "r") as f:
        for ln in f.read().splitlines():
            if ln.strip().startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    return default
PY

# ---------- instrument_store.py (FIX token lookup bug + safer cache handling) ----------
cat > "$ROOT/instrument_store.py" <<'PY'
import os
import pandas as pd
from broker_zerodha import get_kite
from log_store import append_log

INSTR_PATH = os.path.join(os.getcwd(), "cache", "instruments.csv")
os.makedirs(os.path.dirname(INSTR_PATH), exist_ok=True)

def refresh_instruments():
    df = pd.DataFrame(get_kite().instruments("NSE"))
    df.to_csv(INSTR_PATH, index=False)
    append_log("INFO", "INSTR", f"Instruments cached: {len(df)} rows")
    return df

def _load_df():
    if os.path.exists(INSTR_PATH):
        try:
            return pd.read_csv(INSTR_PATH)
        except Exception:
            pass
    return refresh_instruments()

def token_for_symbol(symbol: str) -> int:
    symbol = symbol.strip().upper()
    df = _load_df()
    row = df[df["tradingsymbol"].astype(str).str.upper() == symbol]
    if row.empty:
        # try refresh once
        df = refresh_instruments()
        row = df[df["tradingsymbol"].astype(str).str.upper() == symbol]
        if row.empty:
            raise RuntimeError(f"Symbol {symbol} not found in instruments")
    return int(row.iloc[0]["instrument_token"])
PY

# ---------- strategy_engine.py (Fix: log exceptions, no entry=0, near-signal logs) ----------
cat > "$ROOT/strategy_engine.py" <<'PY'
import time
import pandas as pd

import config as CFG
from broker_zerodha import get_kite
from instrument_store import token_for_symbol
from log_store import append_log


def generate_signal(universe):
    """
    Signal: last close > SMA20 (15m candles). Logs real errors.
    """
    kite = get_kite()

    for sym in universe:
        sym = (sym or "").strip().upper()
        if not sym:
            continue

        try:
            token = token_for_symbol(sym)

            data = kite.historical_data(
                token,
                pd.Timestamp.now() - pd.Timedelta(days=10),
                pd.Timestamp.now(),
                "15minute",
            )

            # Zerodha historical endpoint rate: keep it safe
            time.sleep(0.5)

            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                append_log("WARN", "SIG", f"{sym} no candle data")
                continue

            if len(df) < 25:
                append_log("WARN", "SIG", f"{sym} insufficient candles: {len(df)}")
                continue

            sma20 = df["close"].rolling(20).mean()
            last = float(df["close"].iloc[-1])

            avg_val = sma20.iloc[-1]
            if pd.isna(avg_val):
                append_log("WARN", "SIG", f"{sym} SMA20 NA")
                continue

            avg = float(avg_val)

            if last <= 0 or avg <= 0:
                append_log("WARN", "SIG", f"{sym} invalid prices last={last} sma20={avg}")
                continue

            # Near-signal visibility (within 0.2%)
            if abs(last - avg) / avg < 0.002:
                append_log("INFO", "NEAR", f"{sym} near: last={last:.2f} sma20={avg:.2f}")

            if last > avg:
                append_log("INFO", "SIG", f"{sym} BUY trigger last={last:.2f} sma20={avg:.2f}")
                return {"symbol": sym, "side": "BUY", "entry": float(last)}

        except Exception as e:
            append_log("WARN", "SIG", f"{sym} skipped: {e}")
            continue

    append_log("INFO", "SIG", "No signal found")
    return None
PY

# ---------- night_research.py (writes universe_live atomically; exclusions; csv+report) ----------
cat > "$ROOT/night_research.py" <<'PY'
import os
import time
from datetime import datetime
import pandas as pd
import yfinance as yf

import config as CFG
from log_store import append_log

DATA_DIR = os.path.join(os.getcwd(), "data")
LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

EXCLUSIONS_FILE = os.path.join(DATA_DIR, "exclusions.txt")
NIGHT_SCORES_CSV = os.path.join(DATA_DIR, "night_scores.csv")
NIGHT_REPORT_TXT = os.path.join(LOG_DIR, "night_research_report.txt")

def _load_candidates():
    cand_path = os.path.join(DATA_DIR, "candidates.txt")
    if os.path.exists(cand_path):
        with open(cand_path, "r") as f:
            syms = [ln.strip().upper() for ln in f if ln.strip()]
        if syms:
            return syms

    # fallback to existing universe
    for p in [getattr(CFG, "UNIVERSE_LIVE_PATH", ""), getattr(CFG, "UNIVERSE_TRADING_PATH", ""), getattr(CFG, "UNIVERSE_PATH", "")]:
        if p and os.path.exists(p):
            with open(p, "r") as f:
                syms = [ln.strip().upper() for ln in f if ln.strip()]
            if syms:
                return syms

    return ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LT", "SBIN", "ITC"]

def _load_exclusions():
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])

def fetch_ohlc(sym):
    df = yf.download(
        f"{sym}.NS",
        period="10y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return pd.DataFrame()
    return df

def score_symbol(df):
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if len(df) < 220:
        return None
    close = df["Close"]
    sma200 = close.rolling(200).mean()
    if pd.isna(sma200.iloc[-1]) or float(sma200.iloc[-1]) <= 0:
        return None
    return float((close.iloc[-1] / sma200.iloc[-1]) * 100.0)

def run_night_job():
    start_ts = datetime.now()
    append_log("INFO", "NIGHT", "Night research started")

    candidates = _load_candidates()
    excluded = _load_exclusions()
    candidates = [c for c in candidates if c and c.upper() not in excluded]

    scored = []
    errors = 0

    for sym in candidates:
        try:
            df = fetch_ohlc(sym)
            sc = score_symbol(df)
            if sc is not None:
                scored.append((sym, sc))
            time.sleep(0.4)
        except Exception:
            errors += 1
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    pd.DataFrame(scored, columns=["symbol", "score"]).to_csv(NIGHT_SCORES_CSV, index=False)

    top_n = int(getattr(CFG, "UNIVERSE_SIZE", 30))
    top_syms = [s for s, _ in scored[:top_n]]

    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    os.makedirs(os.path.dirname(live_path), exist_ok=True)

    tmp_path = live_path + ".tmp"
    with open(tmp_path, "w") as f:
        for s in top_syms:
            f.write(s + "\n")
    os.replace(tmp_path, live_path)

    end_ts = datetime.now()
    duration = (end_ts - start_ts).total_seconds()
    report = []
    report.append("🌙 TRIDENT NIGHT RESEARCH REPORT")
    report.append(f"Timestamp: {end_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Candidates scanned: {len(candidates)}")
    report.append(f"Excluded symbols: {len(excluded)}")
    report.append(f"Valid scored: {len(scored)}")
    report.append(f"Errors: {errors}")
    report.append(f"Universe live written: {len(top_syms)} -> {live_path}")
    report.append(f"Scores CSV: {NIGHT_SCORES_CSV}")
    report.append(f"Duration: {duration:.1f}s")
    report.append("")
    report.append("Top 15 symbols:")
    for s, sc in scored[:15]:
        report.append(f"- {s}: {sc:.2f}")

    with open(NIGHT_REPORT_TXT, "w") as f:
        f.write("\n".join(report) + "\n")

    append_log("INFO", "NIGHT", f"Universe live updated: {len(top_syms)} | scored={len(scored)} | errors={errors}")
PY

# ---------- trading_cycle.py (market-hours guard + live safety + slippage guard + auto-promote stable) ----------
cat > "$ROOT/trading_cycle.py" <<'PY'
import os
import time
from datetime import datetime, timedelta
import pandas as pd

import config as CFG
from log_store import append_log
from strategy_engine import generate_signal
from broker_zerodha import get_kite
from instrument_store import token_for_symbol

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)

EXCLUSIONS_FILE = os.path.join(DATA_DIR, "exclusions.txt")

STATE = {
    "paused": True,
    "initiated": False,
    "live_override": False,
    "open_trade": None,
    "today_pnl": 0.0,
    "day_key": datetime.now().strftime("%Y-%m-%d"),
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
}

RUNTIME = {
    "MAX_ENTRY_SLIPPAGE_PCT": float(os.getenv("MAX_ENTRY_SLIPPAGE_PCT", "0.30")),
}

def _parse_hhmm(s):
    try:
        hh, mm = s.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return 0, 0

def _ensure_day_key():
    today = datetime.now().strftime("%Y-%m-%d")
    if STATE.get("day_key") != today:
        STATE["day_key"] = today
        STATE["today_pnl"] = 0.0
        STATE["open_trade"] = None
        append_log("INFO", "DAY", f"Auto rollover reset for {today}")

def set_runtime_param(key, value):
    RUNTIME[key] = value

def manual_reset_day():
    STATE["today_pnl"] = 0.0
    STATE["open_trade"] = None
    STATE["day_key"] = datetime.now().strftime("%Y-%m-%d")
    append_log("INFO", "DAY", "Manual day reset executed")
    return True

def is_live_enabled():
    return bool(STATE.get("initiated")) and bool(CFG.IS_LIVE or STATE.get("live_override"))

def _load_exclusions_set():
    if not os.path.exists(EXCLUSIONS_FILE):
        return set()
    with open(EXCLUSIONS_FILE, "r") as f:
        return set([ln.strip().upper() for ln in f if ln.strip()])

def _save_exclusions_set(s):
    with open(EXCLUSIONS_FILE, "w") as f:
        for sym in sorted(s):
            f.write(sym + "\n")

def list_exclusions():
    s = _load_exclusions_set()
    if not s:
        return "✅ Excluded symbols: (none)"
    return "⛔ Excluded symbols:\n" + "\n".join(sorted(s))

def exclude_symbol(sym):
    sym = sym.strip().upper()
    if not sym:
        return "Usage: /exclude SYMBOL"
    s = _load_exclusions_set()
    s.add(sym)
    _save_exclusions_set(s)
    append_log("WARN", "EXCL", f"Excluded {sym}")
    return f"⛔ {sym} excluded permanently. (/include {sym} to release)"

def include_symbol(sym):
    sym = sym.strip().upper()
    if not sym:
        return "Usage: /include SYMBOL"
    s = _load_exclusions_set()
    if sym in s:
        s.remove(sym)
        _save_exclusions_set(s)
        append_log("INFO", "EXCL", f"Included back {sym}")
        return f"✅ {sym} released from exclusions."
    return f"ℹ️ {sym} was not in exclusions."

def _atomic_copy(src, dst):
    if not os.path.exists(src):
        return False
    ddir = os.path.dirname(dst)
    if ddir:
        os.makedirs(ddir, exist_ok=True)
    tmp = dst + ".tmp"
    with open(src, "r") as fsrc, open(tmp, "w") as fdst:
        fdst.write(fsrc.read())
    os.replace(tmp, dst)
    return True

def _load_universe_from(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]

def load_universe_trading():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))

    if not os.path.exists(trade_path) and os.path.exists(live_path):
        _atomic_copy(live_path, trade_path)
        append_log("INFO", "PROMOTE", "Bootstrapped trading universe from live universe")

    syms = _load_universe_from(trade_path)
    excl = _load_exclusions_set()
    syms = [s for s in syms if s not in excl]

    try:
        maxn = int(getattr(CFG, "UNIVERSE_SIZE", 30))
        syms = syms[:maxn]
    except Exception:
        pass
    return syms

def load_universe_live():
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    syms = _load_universe_from(live_path)
    excl = _load_exclusions_set()
    return [s for s in syms if s not in excl]

def _parse_windows(win_str):
    windows = []
    if not win_str:
        return windows
    parts = [p.strip() for p in win_str.split(",") if p.strip()]
    for p in parts:
        if "-" not in p:
            continue
        a, b = p.split("-", 1)
        ah, am = _parse_hhmm(a)
        bh, bm = _parse_hhmm(b)
        windows.append(((ah, am), (bh, bm)))
    return windows

def _in_any_promote_window():
    now = datetime.now()
    w = _parse_windows(getattr(CFG, "PROMOTE_WINDOWS", ""))
    if not w:
        return False
    for (ah, am), (bh, bm) in w:
        start = now.replace(hour=ah, minute=am, second=0, microsecond=0)
        end = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        if start <= now <= end:
            return True
    return False

def _cooldown_ok():
    cd_min = float(getattr(CFG, "PROMOTE_COOLDOWN_MIN", 60))
    last = STATE.get("last_promote_ts")
    if not last:
        return True
    return (datetime.now() - last) >= timedelta(minutes=cd_min)

def _top10_overlap_ratio(a, b):
    a10 = a[:10]
    b10 = b[:10]
    if not a10 or not b10:
        return 0.0
    inter = len(set(a10).intersection(set(b10)))
    return float(inter) / float(min(len(a10), len(b10)))

def _market_stable():
    try:
        sym = getattr(CFG, "STABILITY_SYMBOL", "NIFTYBEES").strip().upper()
        token = token_for_symbol(sym)
        kite = get_kite()
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=5)
        data = kite.historical_data(token, from_dt, to_dt, "15minute")
        time.sleep(0.3)

        df = pd.DataFrame(data)
        if df.empty or not all(c in df.columns for c in ["high", "low", "close"]):
            return False
        tail = df.tail(10)
        if len(tail) < 8:
            return False

        rng = (tail["high"] - tail["low"]).astype(float)
        close = tail["close"].astype(float)
        rng_pct = (rng / close) * 100.0
        avg_rng_pct = float(rng_pct.mean())

        max_ok = float(getattr(CFG, "STABILITY_ATR_PCT_MAX", 0.35))
        return avg_rng_pct <= max_ok
    except Exception as e:
        append_log("WARN", "STABLE", f"Stability check failed: {e}")
        return False

def promote_universe(reason="AUTO"):
    live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(DATA_DIR, "universe_live.txt"))
    trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(DATA_DIR, "universe_trading.txt"))

    live = _load_universe_from(live_path)
    trade = _load_universe_from(trade_path)

    if not live:
        STATE["last_promote_msg"] = "No live universe available"
        return False

    min_overlap = float(getattr(CFG, "PROMOTE_TOP10_OVERLAP_MIN", 0.60))
    overlap = _top10_overlap_ratio(live, trade) if trade else 1.0

    if trade and overlap < min_overlap:
        STATE["last_promote_msg"] = f"Blocked (overlap {overlap:.2f} < {min_overlap:.2f})"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
        return False

    ok = _atomic_copy(live_path, trade_path)
    if ok:
        STATE["last_promote_ts"] = datetime.now()
        STATE["last_promote_msg"] = f"Promoted ({reason}) overlap={overlap:.2f}"
        append_log("INFO", "PROMOTE", STATE["last_promote_msg"])
        return True

    STATE["last_promote_msg"] = "Promote failed (copy)"
    return False

def get_status_text():
    _ensure_day_key()
    mode = "LIVE ✅" if is_live_enabled() else "PAPER 🟡"
    uni_t = load_universe_trading()
    uni_l = load_universe_live()

    return (
        "📟 Trident Status\n\n"
        f"Mode: {mode}\n"
        f"Paused: {STATE.get('paused')}\n"
        f"Initiated: {STATE.get('initiated')} | LiveOverride: {STATE.get('live_override')}\n"
        f"Universe(trading): {len(uni_t)} symbols\n"
        f"Universe(live): {len(uni_l)} symbols\n"
        f"Today PnL: {float(STATE.get('today_pnl') or 0.0):.2f}\n"
        f"Open Trade: {STATE.get('open_trade')}\n\n"
        "Caps:\n"
        f"- Daily Loss Cap: {CFG.DAILY_LOSS_CAP_INR}\n"
        f"- Daily Profit Target: {CFG.DAILY_PROFIT_TARGET_INR}\n"
        f"- Stoploss %: {CFG.STOPLOSS_PCT}\n"
        f"- Risk/Trade %: {CFG.RISK_PER_TRADE_PCT}\n"
        f"- Tick Seconds: {CFG.TICK_SECONDS}\n"
        f"- Max Slippage %: {RUNTIME.get('MAX_ENTRY_SLIPPAGE_PCT')}\n\n"
        f"AutoPromote: {getattr(CFG, 'AUTO_PROMOTE_ENABLED', False)} | Last: {STATE.get('last_promote_msg')}\n"
    )

def _calc_qty(price):
    capital = float(CFG.CAPITAL_INR)
    risk_amt = capital * float(CFG.RISK_PER_TRADE_PCT) / 100.0
    per_share_risk = price * float(CFG.STOPLOSS_PCT) / 100.0
    if per_share_risk <= 0:
        return 1
    risk_qty = int(risk_amt / per_share_risk)
    affordable_qty = int(capital / price) if price > 0 else 0
    return max(1, min(risk_qty, affordable_qty))

def _ltp(kite, sym):
    try:
        ins = f"{CFG.EXCHANGE}:{sym}"
        data = kite.ltp([ins])
        return float(data[ins]["last_price"])
    except Exception:
        return None

def _place_live_order(kite, sym, side, qty):
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=CFG.EXCHANGE,
            tradingsymbol=sym,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=CFG.PRODUCT,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        return order_id
    except Exception as e:
        append_log("ERROR", "ORDER", f"Order failed {sym} {side} qty={qty}: {e}")
        return None

def _close_open_trade(reason="MANUAL"):
    trade = STATE.get("open_trade")
    if not trade:
        return False
    sym = trade.get("symbol")
    side = trade.get("side")
    qty = int(trade.get("qty") or 0) or 1
    exit_side = "SELL" if side == "BUY" else "BUY"

    if not is_live_enabled():
        append_log("WARN", "EXIT", f"PAPER exit {sym} ({reason})")
        STATE["open_trade"] = None
        return True

    kite = get_kite()
    oid = _place_live_order(kite, sym, exit_side, qty)
    if oid:
        append_log("WARN", "EXIT", f"LIVE exit {sym} ({reason}) order_id={oid}")
        STATE["open_trade"] = None
        return True
    return False

def _within_entry_window():
    now = datetime.now()
    sh, sm = _parse_hhmm(getattr(CFG, "ENTRY_START", "09:20"))
    eh, em = _parse_hhmm(getattr(CFG, "ENTRY_END", "14:30"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end

def tick():
    _ensure_day_key()
    if STATE.get("paused"):
        return

    if STATE["today_pnl"] <= -abs(CFG.DAILY_LOSS_CAP_INR):
        append_log("WARN", "CAP", "Daily loss cap hit. Pausing loop.")
        STATE["paused"] = True
        return

    if STATE["today_pnl"] >= abs(CFG.DAILY_PROFIT_TARGET_INR):
        append_log("INFO", "CAP", "Daily profit target hit. Pausing loop.")
        STATE["paused"] = True
        return

    if (
        getattr(CFG, "AUTO_PROMOTE_ENABLED", False)
        and STATE.get("open_trade") is None
        and _in_any_promote_window()
        and _cooldown_ok()
    ):
        if _market_stable():
            promote_universe(reason="AUTO_STABLE")
        else:
            STATE["last_promote_msg"] = "Skipped promote: market not stable"

    if not _within_entry_window():
        return

    if STATE.get("open_trade"):
        return

    universe = load_universe_trading()
    if not universe:
        append_log("WARN", "UNIV", "Trading universe empty. Run /nightnow or ensure live universe exists.")
        return

    sig = generate_signal(universe)
    if not sig:
        return

    sym = sig["symbol"].strip().upper()
    entry = float(sig.get("entry") or 0.0)
    if entry <= 0:
        append_log("WARN", "TRADE", f"Invalid signal entry for {sym}: {entry}")
        return

    qty = _calc_qty(entry)
    sl_price = entry * (1.0 - float(CFG.STOPLOSS_PCT) / 100.0)

    if is_live_enabled():
        kite = get_kite()
        now_price = _ltp(kite, sym)
        if now_price is not None:
            sig_price = entry
            if sig_price <= 0:
                append_log("WARN", "SLIP", f"Skip {sym}: invalid sig price {sig_price}")
                return
            max_slip = float(RUNTIME.get("MAX_ENTRY_SLIPPAGE_PCT", 0.30)) / 100.0
            if now_price > sig_price * (1.0 + max_slip):
                append_log("WARN", "SLIP", f"Skip {sym}: slip too high now={now_price} sig={sig_price}")
                return

        oid = _place_live_order(kite, sym, "BUY", qty)
        if not oid:
            return

        STATE["open_trade"] = {
            "symbol": sym,
            "side": "BUY",
            "entry": entry,
            "qty": qty,
            "order_id": oid,
            "sl_price": sl_price,
            "peak": entry,
        }
        append_log("INFO", "TRADE", f"LIVE Entered {sym} qty={qty} entry={entry} sl={sl_price} oid={oid}")
    else:
        STATE["open_trade"] = {
            "symbol": sym,
            "side": "BUY",
            "entry": entry,
            "qty": qty,
            "order_id": None,
            "sl_price": sl_price,
            "peak": entry,
        }
        append_log("INFO", "TRADE", f"PAPER Entered {sym} qty={qty} entry={entry} sl={sl_price}")

def run_loop_forever():
    append_log("INFO", "LOOP", "Trading loop started")
    while True:
        try:
            tick()
        except Exception as e:
            append_log("ERROR", "LOOP", str(e))
        time.sleep(int(CFG.TICK_SECONDS))
PY

# ---------- bot.py (roles + /myid + add/remove users + safer admin gating) ----------
cat > "$ROOT/bot.py" <<'PY'
import asyncio
import os
from datetime import datetime, timedelta, time as dtime

import config as CFG
import trading_cycle as CYCLE
import night_research

from telethon import TelegramClient, events
from kiteconnect import KiteConnect

from log_store import append_log, tail_text, export_all, LOG_FILE
from env_utils import set_env_value, get_env_value
from broker_zerodha import get_kite


HELP_TEXT = (
    "🤖 TRIDENT BOT – COMMANDS\n\n"
    "ACCESS:\n"
    "• /myid                → shows your Telegram ID\n"
    "• (Owner) /addtrader <id>, /removetrader <id>\n"
    "• (Owner) /addviewer <id>, /removeviewer <id>\n\n"
    "TOKEN (Zerodha daily) [Owner]:\n"
    "• /renewtoken            → sends Zerodha login link\n"
    "• /token <request_token> → generates access token + saves to .env\n"
    "   After /token, restart: sudo systemctl restart trident\n\n"
    "LIVE SAFETY [Owner]:\n"
    "• /initiate (or /arm)     → enables LIVE immediately (runtime override)\n"
    "• /disengage (or /disarm) → stops LIVE immediately\n\n"
    "LOOP [Trader/Owner]:\n"
    "• /startloop → start trading loop\n"
    "• /stoploop  → pause trading loop\n\n"
    "MONITOR [Viewer+]:\n"
    "• /status     → status + daily caps\n"
    "• /logs       → last 20 log lines\n"
    "• /exportlog  → full log as txt\n"
    "• /dailylog   → today's log as txt\n"
    "• /positions  → Zerodha net positions\n\n"
    "RESEARCH [Trader/Owner]:\n"
    "• /nightnow       → rebuild live universe now\n"
    "• /universe       → show TRADING universe\n"
    "• /universe_live  → show LIVE universe\n"
    "• /nightreport    → research report summary\n"
    "• /nightlog       → recent NIGHT log lines\n\n"
    "AUTO-PROMOTE [Trader/Owner]:\n"
    "• /promotestatus  → last promote status\n"
    "• /promote_now    → manual promote live→trading (only if flat)\n\n"
    "SLIPPAGE [Trader/Owner]:\n"
    "• /setslip X → MAX_ENTRY_SLIPPAGE_PCT (example: /setslip 0.30)\n\n"
    "INSIDER SAFETY [Owner]:\n"
    "• /exclude SBIN   → permanently block symbol\n"
    "• /include SBIN   → release symbol\n"
    "• /excluded       → list blocked symbols\n\n"
    "EMERGENCY [Owner]:\n"
    "• /panic     → pause + disengage + close open trade\n"
    "• /resetday  → reset today's pnl & risk counters\n"
)


def _parse_ids(csv_text):
    out = set()
    if not csv_text:
        return out
    for p in csv_text.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out


def _owner_id():
    # Prefer OWNER_USER_ID; fallback to ADMIN_USER_ID
    o = os.getenv("OWNER_USER_ID", "").strip()
    if not o:
        o = os.getenv("ADMIN_USER_ID", "").strip()
    try:
        return int(o) if o else 0
    except Exception:
        return 0


def _role_sets():
    owner = _owner_id()

    traders = _parse_ids(os.getenv("TRADER_USER_IDS", ""))
    viewers = _parse_ids(os.getenv("VIEWER_USER_IDS", ""))

    # backward compatibility: if only ADMIN_USER_ID exists, treat as owner+trader+viewer
    if owner:
        traders.add(owner)
        viewers.add(owner)

    return owner, traders, viewers


def _is_owner(sender_id):
    owner, _, _ = _role_sets()
    return int(sender_id) == int(owner)


def _is_trader(sender_id):
    _, traders, _ = _role_sets()
    return int(sender_id) in traders


def _is_viewer(sender_id):
    owner, traders, viewers = _role_sets()
    sid = int(sender_id)
    return (sid == int(owner)) or (sid in traders) or (sid in viewers)


def _is_private(event):
    try:
        return bool(event.is_private)
    except Exception:
        return False


def _make_daily_log_file():
    if not os.path.exists(LOG_FILE):
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "trident_daily_%s.txt" % today)
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    todays = [ln for ln in lines if ln.startswith(today)]
    if not todays:
        return None
    with open(out_path, "w") as f:
        f.writelines(todays)
    return out_path


def _tail_night_lines(n=120):
    txt = tail_text(n * 5)
    if not txt:
        return "(no logs)"
    lines = [ln for ln in txt.splitlines() if "[NIGHT]" in ln]
    return "\n".join(lines[-n:]) if lines else "(no NIGHT lines yet)"


def _read_universe(path, limit=40):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r") as f:
        syms = [ln.strip().upper() for ln in f if ln.strip()]
    return syms[:limit]


def _parse_float(s):
    try:
        return float(s.strip())
    except Exception:
        return None


def _in_time_range(now_t, start_t, end_t):
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


async def night_scheduler():
    enabled = str(os.getenv("NIGHT_AUTO_ENABLED", "true")).lower() == "true"
    if not enabled:
        append_log("INFO", "NIGHT", "Night scheduler disabled")
        return

    ns = os.getenv("NIGHT_START", "18:30")
    ih, im = [int(x) for x in ns.split(":")]
    start_t = dtime(ih, im)

    es = os.getenv("ENTRY_START", "09:20")
    eh, em = [int(x) for x in es.split(":")]
    offset = int(os.getenv("NIGHT_END_OFFSET_MIN", "5"))
    end_dt = (datetime.now().replace(hour=eh, minute=em, second=0, microsecond=0) - timedelta(minutes=offset))
    end_t = end_dt.time()

    interval_min = int(os.getenv("NIGHT_INTERVAL_MIN", "90"))

    append_log("INFO", "NIGHT", "Night scheduler active")

    while True:
        try:
            now = datetime.now()
            if _in_time_range(now.time(), start_t, end_t):
                append_log("INFO", "NIGHT", "Auto scheduler triggering night research")
                await asyncio.to_thread(night_research.run_night_job)
                await asyncio.sleep(interval_min * 60)
            else:
                await asyncio.sleep(10 * 60)
        except Exception as e:
            append_log("ERROR", "NIGHT", "Scheduler error: %s" % e)
            await asyncio.sleep(10 * 60)


def _update_id_list_env(key, user_id, add=True):
    """
    Updates comma-separated list in .env and process env (runtime).
    """
    user_id = int(user_id)
    current = get_env_value(key, os.getenv(key, "")).strip()
    s = _parse_ids(current)
    if add:
        s.add(user_id)
    else:
        if user_id in s:
            s.remove(user_id)
    new_val = ",".join([str(x) for x in sorted(s)])
    set_env_value(key, new_val)
    os.environ[key] = new_val
    return new_val


async def main():
    api_id = int(getattr(CFG, "TELEGRAM_API_ID", 9888950))
    api_hash = getattr(CFG, "TELEGRAM_API_HASH", "ecfa673e2c85b4ef16743acf0ba0d1c1")

    if not CFG.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")

    client = TelegramClient("trident", api_id, api_hash)
    await client.start(bot_token=CFG.TELEGRAM_BOT_TOKEN)
    append_log("INFO", "BOT", "Telegram bot started")

    @client.on(events.NewMessage())
    async def handler(event):
        if not _is_private(event):
            return

        sender = int(event.sender_id)
        cmd = (event.raw_text or "").strip()

        # Always allow /myid
        if cmd == "/myid":
            await event.reply(f"🆔 Your Telegram ID: `{sender}`")
            return

        # Viewer gate for everything else
        if not _is_viewer(sender):
            return

        if cmd in ("/help", "/commands"):
            await event.reply(HELP_TEXT)
            return

        if cmd == "/status":
            await event.reply(CYCLE.get_status_text())
            return

        # ===== Owner user management =====
        if cmd.startswith("/addtrader ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=True)
                await event.reply(f"✅ Added trader {uid}\nTRADER_USER_IDS={newv}\n(Changes apply immediately)")
            else:
                await event.reply("Usage: /addtrader 123456789")
            return

        if cmd.startswith("/removetrader ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("TRADER_USER_IDS", int(uid), add=False)
                await event.reply(f"✅ Removed trader {uid}\nTRADER_USER_IDS={newv}")
            else:
                await event.reply("Usage: /removetrader 123456789")
            return

        if cmd.startswith("/addviewer ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=True)
                await event.reply(f"✅ Added viewer {uid}\nVIEWER_USER_IDS={newv}\n(Changes apply immediately)")
            else:
                await event.reply("Usage: /addviewer 123456789")
            return

        if cmd.startswith("/removeviewer ") and _is_owner(sender):
            uid = cmd.split(maxsplit=1)[1].strip()
            if uid.isdigit():
                newv = _update_id_list_env("VIEWER_USER_IDS", int(uid), add=False)
                await event.reply(f"✅ Removed viewer {uid}\nVIEWER_USER_IDS={newv}")
            else:
                await event.reply("Usage: /removeviewer 123456789")
            return

        # ===== Trader gated commands =====
        if cmd == "/startloop":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            CYCLE.STATE["paused"] = False
            await event.reply("▶️ Loop Started")
            return

        if cmd == "/stoploop":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            CYCLE.STATE["paused"] = True
            await event.reply("⏸️ Loop Paused")
            return

        # ===== Owner-only LIVE safety =====
        if cmd in ("/initiate", "/arm"):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["initiated"] = True
            CYCLE.STATE["live_override"] = True
            await event.reply("🟢 LIVE INITIATED (runtime override enabled). Use /disengage to stop.")
            return

        if cmd in ("/disengage", "/disarm"):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            await event.reply("🔴 DISENGAGED (runtime override disabled). Orders blocked.")
            return

        # ===== Logs (Viewer+) =====
        if cmd == "/logs":
            await event.reply(tail_text(20) or "(no logs)")
            return

        if cmd == "/exportlog":
            fp = export_all()
            if not fp or not os.path.exists(fp):
                await event.reply("No log file found.")
                return
            await event.reply("📤 Sending full log (txt)…")
            await client.send_file(event.chat_id, fp)
            return

        if cmd == "/dailylog":
            fp = _make_daily_log_file()
            if not fp:
                await event.reply("No logs for today yet.")
                return
            await event.reply("📤 Sending today's log (txt)…")
            await client.send_file(event.chat_id, fp)
            return

        # ===== Positions (Viewer+) =====
        if cmd == "/positions":
            try:
                kite = get_kite()
                pos = kite.positions() or {}
                net = pos.get("net", []) or []
                rows = []
                for p in net:
                    qty = int(p.get("quantity") or 0)
                    if qty == 0:
                        continue
                    rows.append(
                        "%s:%s qty=%s avg=%.2f pnl=%.2f" % (
                            p.get("exchange"),
                            p.get("tradingsymbol"),
                            qty,
                            float(p.get("average_price") or 0.0),
                            float(p.get("pnl") or 0.0),
                        )
                    )
                await event.reply("📊 Net Positions\n\n" + ("\n".join(rows) if rows else "None"))
            except Exception as e:
                await event.reply("❌ Positions failed: %s" % e)
            return

        # ===== Research (Trader/Owner) =====
        if cmd == "/nightnow":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            await event.reply("🌙 Running Night Research…")
            try:
                await asyncio.to_thread(night_research.run_night_job)
                await event.reply("✅ Done. Use /universe_live or /nightreport.")
            except Exception as e:
                await event.reply("❌ Night research failed: %s" % e)
            return

        if cmd == "/nightlog":
            await event.reply("🌙 Night Logs (recent)\n\n" + _tail_night_lines(120))
            return

        if cmd == "/nightreport":
            rpt = os.path.join(os.getcwd(), "logs", "night_research_report.txt")
            if os.path.exists(rpt):
                with open(rpt, "r") as f:
                    txt = f.read()
                await event.reply(txt[-3500:])
            else:
                await event.reply("No night report yet. Run /nightnow")
            return

        if cmd == "/universe":
            trade_path = getattr(CFG, "UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))
            syms = _read_universe(trade_path, 50)
            await event.reply("📌 TRADING Universe (%d)\n\n%s" % (len(syms), "\n".join(syms) if syms else "(empty)"))
            return

        if cmd == "/universe_live":
            live_path = getattr(CFG, "UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
            syms = _read_universe(live_path, 50)
            await event.reply("📈 LIVE Universe (%d)\n\n%s" % (len(syms), "\n".join(syms) if syms else "(empty)"))
            return

        # ===== Promote (Trader/Owner) =====
        if cmd == "/promotestatus":
            msg = "Last promote: %s" % (CYCLE.STATE.get("last_promote_msg") or "N/A")
            await event.reply("🔄 Promote Status\n\n" + msg)
            return

        if cmd == "/promote_now":
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            if CYCLE.STATE.get("open_trade"):
                await event.reply("❌ Cannot promote while in open trade.")
                return
            ok = CYCLE.promote_universe(reason="MANUAL")
            await event.reply("✅ Promoted live→trading" if ok else ("❌ Promote blocked: " + (CYCLE.STATE.get("last_promote_msg") or "")))
            return

        # ===== Slippage (Trader/Owner) =====
        if cmd.startswith("/setslip "):
            if not _is_trader(sender):
                await event.reply("❌ Not permitted (Trader/Owner only).")
                return
            v = _parse_float(cmd.split(maxsplit=1)[1])
            if v is None or v < 0:
                await event.reply("Usage: /setslip 0.30")
                return
            set_env_value("MAX_ENTRY_SLIPPAGE_PCT", str(v))
            os.environ["MAX_ENTRY_SLIPPAGE_PCT"] = str(v)
            CYCLE.set_runtime_param("MAX_ENTRY_SLIPPAGE_PCT", float(v))
            await event.reply("✅ MAX_ENTRY_SLIPPAGE_PCT set to %s (restart optional)" % v)
            return

        # ===== Insider safety (Owner only) =====
        if cmd == "/excluded":
            await event.reply(CYCLE.list_exclusions())
            return

        if cmd.startswith("/exclude "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.exclude_symbol(sym))
            return

        if cmd.startswith("/include "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            sym = cmd.split(maxsplit=1)[1].strip().upper()
            await event.reply(CYCLE.include_symbol(sym))
            return

        # ===== Emergency (Owner only) =====
        if cmd == "/panic":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.STATE["paused"] = True
            CYCLE.STATE["initiated"] = False
            CYCLE.STATE["live_override"] = False
            CYCLE._close_open_trade("PANIC")
            await event.reply("🛑 PANIC done: paused + disengaged + attempted close.")
            return

        if cmd == "/resetday":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            CYCLE.manual_reset_day()
            await event.reply("✅ Day reset done.")
            return

        # ===== Token flow (Owner only) =====
        if cmd == "/renewtoken":
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            if not getattr(CFG, "KITE_LOGIN_URL", ""):
                await event.reply("❌ KITE_LOGIN_URL missing in .env")
                return
            await event.reply(
                "🔑 Renew Zerodha Session\n\n"
                "1) Open this link & login:\n%s\n\n"
                "2) Copy request_token from redirect URL\n"
                "3) Send:\n/token YOUR_REQUEST_TOKEN" % CFG.KITE_LOGIN_URL
            )
            return

        if cmd.startswith("/token "):
            if not _is_owner(sender):
                await event.reply("❌ Not permitted (Owner only).")
                return
            req_token = cmd.split(" ", 1)[1].strip()
            if not getattr(CFG, "KITE_API_KEY", ""):
                await event.reply("❌ KITE_API_KEY missing in .env")
                return
            api_secret = os.getenv("KITE_API_SECRET", "").strip()
            if not api_secret:
                await event.reply("❌ KITE_API_SECRET missing in .env (add it and restart)")
                return
            try:
                kite = KiteConnect(api_key=CFG.KITE_API_KEY)
                data = kite.generate_session(req_token, api_secret=api_secret)
                access = data["access_token"]
                set_env_value("KITE_ACCESS_TOKEN", access)
                await event.reply("✅ Access token updated in .env. Now run: sudo systemctl restart trident")
            except Exception as e:
                await event.reply("❌ Token update failed: %s" % e)
            return

        await event.reply("Unknown command. Use /help")

    await asyncio.gather(
        client.run_until_disconnected(),
        asyncio.to_thread(CYCLE.run_loop_forever),
        night_scheduler(),
    )


if __name__ == "__main__":
    asyncio.run(main())
PY

# ---------- config.py (append new keys if helper functions exist; safe no-op if already present) ----------
if [ -f "$ROOT/config.py" ]; then
  if grep -q "UNIVERSE_LIVE_PATH" "$ROOT/config.py"; then
    echo "==> config.py already has new universe keys"
  else
    echo "==> Appending new config keys to config.py"
    cat >> "$ROOT/config.py" <<'PY'

# ===== Trident PATCH V2: Universe + Auto Promote + Night Scheduler + Roles =====
UNIVERSE_LIVE_PATH = os.getenv("UNIVERSE_LIVE_PATH", os.path.join(os.getcwd(), "data", "universe_live.txt"))
UNIVERSE_TRADING_PATH = os.getenv("UNIVERSE_TRADING_PATH", os.path.join(os.getcwd(), "data", "universe_trading.txt"))

AUTO_PROMOTE_ENABLED = os.getenv("AUTO_PROMOTE_ENABLED", "true").lower().strip() == "true"
PROMOTE_COOLDOWN_MIN = int(os.getenv("PROMOTE_COOLDOWN_MIN", "60"))
PROMOTE_WINDOWS = os.getenv("PROMOTE_WINDOWS", "10:00-10:10,12:00-12:10,13:30-13:40")
PROMOTE_TOP10_OVERLAP_MIN = float(os.getenv("PROMOTE_TOP10_OVERLAP_MIN", "0.60"))
STABILITY_ATR_PCT_MAX = float(os.getenv("STABILITY_ATR_PCT_MAX", "0.35"))
STABILITY_SYMBOL = os.getenv("STABILITY_SYMBOL", "NIFTYBEES")

NIGHT_AUTO_ENABLED = os.getenv("NIGHT_AUTO_ENABLED", "true").lower().strip() == "true"
NIGHT_START = os.getenv("NIGHT_START", "18:30")
NIGHT_INTERVAL_MIN = int(os.getenv("NIGHT_INTERVAL_MIN", "90"))
NIGHT_END_OFFSET_MIN = int(os.getenv("NIGHT_END_OFFSET_MIN", "5"))

# Roles (bot.py enforces; config values optional)
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", os.getenv("ADMIN_USER_ID", "0") or "0"))
TRADER_USER_IDS = os.getenv("TRADER_USER_IDS", "")
VIEWER_USER_IDS = os.getenv("VIEWER_USER_IDS", "")
PY
  fi
else
  echo "WARN: config.py not found; skipping append"
fi

# ---------- compile check ----------
echo "==> Python compile check"
PYBIN="$ROOT/venv/bin/python"
if [ ! -x "$PYBIN" ]; then
  PYBIN="python3"
fi

$PYBIN -m py_compile "$ROOT/bot.py"
$PYBIN -m py_compile "$ROOT/trading_cycle.py"
$PYBIN -m py_compile "$ROOT/night_research.py"
$PYBIN -m py_compile "$ROOT/strategy_engine.py"
$PYBIN -m py_compile "$ROOT/env_utils.py"
$PYBIN -m py_compile "$ROOT/instrument_store.py"
echo "==> Compile OK"

# ---------- restart service ----------
echo "==> Restarting systemd service trident"
sudo systemctl daemon-reload || true
sudo systemctl restart trident
sudo systemctl status trident -n 25 --no-pager || true

echo ""
echo "✅ PATCH V2 complete."
echo "Backup located at: $BACKUP_DIR"
echo ""
echo "Next tests (Telegram):"
echo "  /myid"
echo "  /help"
echo "  /status"
echo "  /nightnow  then  /universe_live"
echo "  /promotestatus"
PY
