import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from log_store import append_log

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
TRADE_HISTORY_PATH = os.path.join(DATA_DIR, "trade_history.csv")
STRATEGY_STATS_PATH = os.path.join(DATA_DIR, "strategy_stats.json")
SKIPPED_SIGNALS_PATH = os.path.join(DATA_DIR, "skipped_signals.csv")


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _safe_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        if d is None:
            return None
        return float(d)


def _read_csv_rows(path: str):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r:
                    out.append(dict(r))
    except Exception:
        return []
    return out


def _append_csv(path: str, fieldnames: list[str], row: dict):
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})


def record_trade_exit(record: dict):
    fields = [
        "entry_time", "exit_time", "symbol", "side", "qty", "entry", "exit", "pnl_inr", "pnl_pct", "reason",
        "strategy_tag", "market_regime", "universe_source", "sector",
    ]
    row = dict(record or {})
    row.setdefault("exit_time", _now_iso())
    _append_csv(TRADE_HISTORY_PATH, fields, row)


def record_skipped_signal(record: dict):
    fields = [
        "time", "symbol", "side", "reason", "strategy_tag", "market_regime", "signal_price", "after_15m_pct", "after_30m_pct", "after_60m_pct"
    ]
    row = dict(record or {})
    row.setdefault("time", _now_iso())
    _append_csv(SKIPPED_SIGNALS_PATH, fields, row)


def _calc_stats(rows: list[dict]) -> dict:
    pnl = [_safe_float(r.get("pnl_inr")) for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    n = len(pnl)
    net = sum(pnl)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    win_rate = (len(wins) * 100.0 / n) if n else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and abs(sum(losses)) > 0 else (999.0 if wins else 0.0)
    expectancy = (win_rate / 100.0 * avg_win) + ((1.0 - win_rate / 100.0) * avg_loss)
    # simple equity DD from sequence
    eq, peak, max_dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "net_pnl": round(net, 2),
        "profit_factor": round(profit_factor, 3),
        "expectancy": round(expectancy, 2),
        "recent_pnl": round(sum(pnl[-10:]), 2),
        "max_drawdown": round(abs(max_dd), 2),
    }


def rebuild_strategy_stats() -> dict:
    rows = _read_csv_rows(TRADE_HISTORY_PATH)
    by_strategy = defaultdict(list)
    by_regime = defaultdict(list)
    by_sector = defaultdict(list)
    by_symbol = defaultdict(list)
    for r in rows:
        by_strategy[str(r.get("strategy_tag") or "unknown")].append(r)
        by_regime[str(r.get("market_regime") or "UNKNOWN")].append(r)
        by_sector[str(r.get("sector") or "OTHER")].append(r)
        by_symbol[str(r.get("symbol") or "")].append(r)

    out = {
        "updated_at": _now_iso(),
        "strategy": {k: _calc_stats(v) for k, v in by_strategy.items()},
        "regime": {k: _calc_stats(v) for k, v in by_regime.items()},
        "sector": {k: _calc_stats(v) for k, v in by_sector.items()},
        "symbol": {k: _calc_stats(v) for k, v in by_symbol.items()},
    }
    try:
        with open(STRATEGY_STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        append_log("WARN", "ALLOC", f"failed to write strategy stats: {e}")
    return out


def load_strategy_stats() -> dict:
    if not os.path.exists(STRATEGY_STATS_PATH):
        return rebuild_strategy_stats()
    try:
        with open(STRATEGY_STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return rebuild_strategy_stats()


def _optimal_f_multiplier(trades: list[dict], cfg) -> float:
    min_n = int(getattr(cfg, "MIN_TRADES_FOR_OPTIMAL_F", 30) or 30)
    if len(trades) < min_n:
        return 1.0
    rets = []
    for t in trades:
        entry = _safe_float(t.get("entry"), 0.0)
        pnl = _safe_float(t.get("pnl_inr"), 0.0)
        qty = max(1.0, _safe_float(t.get("qty"), 1.0))
        stake = max(1.0, entry * qty)
        rets.append(pnl / stake)
    if not rets:
        return 1.0
    # conservative growth-opt search
    best_f, best_g = 0.0, -1e18
    for i in range(1, 41):
        f = i / 100.0
        g = 0.0
        valid = True
        for r in rets:
            x = 1.0 + f * r
            if x <= 0:
                valid = False
                break
            g += math.log(x)
        if valid and g > best_g:
            best_g = g
            best_f = f
    frac = float(getattr(cfg, "OPTIMAL_F_FRACTION", 0.25) or 0.25)
    raw = max(0.0, best_f * frac / 0.1)  # normalize around 10%
    lo = float(getattr(cfg, "OPTIMAL_F_MIN_MULTIPLIER", 0.25) or 0.25)
    hi = float(getattr(cfg, "OPTIMAL_F_MAX_MULTIPLIER", 1.25) or 1.25)
    return max(lo, min(hi, raw if raw > 0 else 1.0))


def get_strategy_multiplier(strategy_tag: str, cfg) -> tuple[float, str]:
    rows = _read_csv_rows(TRADE_HISTORY_PATH)
    tag_rows = [r for r in rows if str(r.get("strategy_tag") or "") == str(strategy_tag or "")]

    min_n = int(getattr(cfg, "MIN_TRADES_FOR_ALLOCATION", 20) or 20)
    if len(tag_rows) < min_n:
        return 1.0, "insufficient_history"

    s = _calc_stats(tag_rows)
    exp_full = float(getattr(cfg, "EXPECTANCY_FULL_SIZE", 50.0) or 50.0)
    exp_half = float(getattr(cfg, "EXPECTANCY_HALF_SIZE", 10.0) or 10.0)
    disable_n = int(getattr(cfg, "DISABLE_NEGATIVE_LAST_N", 10) or 10)
    recent = [_safe_float(r.get("pnl_inr"), 0.0) for r in tag_rows[-disable_n:]]
    if recent and sum(recent) < 0:
        base = 0.0
        reason = "negative_recent"
    elif s["expectancy"] >= exp_full:
        base = 1.0
        reason = "strong_expectancy"
    elif s["expectancy"] >= exp_half:
        base = 0.5
        reason = "moderate_expectancy"
    else:
        base = 0.0
        reason = "low_expectancy"

    if bool(getattr(cfg, "USE_OPTIMAL_F", True)) and base > 0:
        of = _optimal_f_multiplier(tag_rows, cfg)
        base = base * of
        reason += f"+optimal_f({of:.2f})"
    return max(0.0, float(base)), reason


def strategy_report_text(limit: int = 8) -> str:
    stats = load_strategy_stats().get("strategy", {})
    if not stats:
        return "📊 Strategy Report\n\nNo strategy stats yet."
    items = sorted(stats.items(), key=lambda kv: float(kv[1].get("net_pnl", 0.0)), reverse=True)[:limit]
    lines = ["📊 Strategy Report", ""]
    for k, v in items:
        lines.append(
            f"{k}: trades={v.get('trades',0)} win={float(v.get('win_rate',0)):.1f}% net=₹{float(v.get('net_pnl',0)):.2f} exp={float(v.get('expectancy',0)):.2f}"
        )
    return "\n".join(lines)


def best_worst_strategy() -> tuple[str, str]:
    stats = load_strategy_stats().get("strategy", {})
    if not stats:
        return "No strategy data", "No strategy data"
    items = sorted(stats.items(), key=lambda kv: float(kv[1].get("net_pnl", 0.0)))
    worst = items[0]
    best = items[-1]
    return f"🏆 Best: {best[0]} net=₹{float(best[1].get('net_pnl',0)):.2f}", f"⚠️ Worst: {worst[0]} net=₹{float(worst[1].get('net_pnl',0)):.2f}"


def regime_report_text(limit: int = 8) -> str:
    stats = load_strategy_stats().get("regime", {})
    if not stats:
        return "📈 Regime Report\n\nNo regime stats yet."
    items = sorted(stats.items(), key=lambda kv: float(kv[1].get("net_pnl", 0.0)), reverse=True)[:limit]
    return "📈 Regime Report\n\n" + "\n".join(
        f"{k}: trades={v.get('trades',0)} net=₹{float(v.get('net_pnl',0)):.2f} win={float(v.get('win_rate',0)):.1f}%" for k, v in items
    )


def sector_report_text(limit: int = 8) -> str:
    stats = load_strategy_stats().get("sector", {})
    if not stats:
        return "🏭 Sector Report\n\nNo sector stats yet."
    items = sorted(stats.items(), key=lambda kv: float(kv[1].get("net_pnl", 0.0)), reverse=True)[:limit]
    return "🏭 Sector Report\n\n" + "\n".join(
        f"{k}: trades={v.get('trades',0)} net=₹{float(v.get('net_pnl',0)):.2f} win={float(v.get('win_rate',0)):.1f}%" for k, v in items
    )


def _today_str():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _today_rows(rows: list[dict], key: str = "exit_time"):
    d = _today_str()
    out = []
    for r in rows:
        t = str(r.get(key) or "")
        if t[:10] == d:
            out.append(r)
    return out


def generate_eod_report_text(state: dict) -> str:
    append_log("INFO", "EOD", "Generating daily summary")
    trades = _today_rows(_read_csv_rows(TRADE_HISTORY_PATH), key="exit_time")
    skips = _today_rows(_read_csv_rows(SKIPPED_SIGNALS_PATH), key="time")
    append_log("INFO", "EOD", "Calculating strategy stats")
    stats = rebuild_strategy_stats()

    pnl = [_safe_float(t.get("pnl_inr")) for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    n = len(trades)
    net = sum(pnl)
    wr = (len(wins) * 100.0 / n) if n else 0.0
    avg_w = (sum(wins) / len(wins)) if wins else 0.0
    avg_l = (sum(losses) / len(losses)) if losses else 0.0
    best = max(trades, key=lambda r: _safe_float(r.get("pnl_inr")), default=None)
    worst = min(trades, key=lambda r: _safe_float(r.get("pnl_inr")), default=None)

    by_strategy = defaultdict(float)
    by_sector = defaultdict(float)
    by_reason = defaultdict(int)
    for t in trades:
        by_strategy[str(t.get("strategy_tag") or "unknown")] += _safe_float(t.get("pnl_inr"))
        by_sector[str(t.get("sector") or "OTHER")] += _safe_float(t.get("pnl_inr"))
    for s in skips:
        by_reason[str(s.get("reason") or "unknown")] += 1

    missed = []
    for s in skips:
        vals = [_safe_float(s.get("after_15m_pct"), None), _safe_float(s.get("after_30m_pct"), None), _safe_float(s.get("after_60m_pct"), None)]
        vals = [v for v in vals if v is not None]
        if vals:
            missed.append((str(s.get("symbol") or ""), str(s.get("side") or "BUY"), str(s.get("reason") or ""), max(vals)))
    missed = sorted(missed, key=lambda x: x[3], reverse=True)[:2]

    append_log("INFO", "EOD", "Generating insights")
    insights = []
    if n == 0:
        insights.append("No trades executed; bot remained defensive under active filters.")
    if net > 0 and wr < 50:
        insights.append("Low win rate but positive expectancy due to stronger average winners.")
    if by_strategy:
        best_st = max(by_strategy.items(), key=lambda x: x[1])
        insights.append(f"{best_st[0]} was the strongest contributor today.")
    if by_reason:
        top_skip = max(by_reason.items(), key=lambda x: x[1])
        insights.append(f"Most skipped signals were due to {top_skip[0]} ({top_skip[1]}).")
    if by_sector:
        top_sec = max(by_sector.items(), key=lambda x: x[1])
        insights.append(f"Sector leadership concentrated in {top_sec[0]}.")
    insights = insights[:6]

    def _fmt_p(x):
        return f"+₹{x:.2f}" if x >= 0 else f"-₹{abs(x):.2f}"

    lines = [
        "📊 TRIDENT DAILY REPORT",
        "",
        f"Date: {_today_str()}",
        "",
        f"Trades: {n}",
        f"Wins: {len(wins)}",
        f"Losses: {len(losses)}",
        f"Win Rate: {wr:.1f}%",
        "",
        f"💰 Net PnL: {_fmt_p(net)}",
        f"Avg Win: ₹{avg_w:.2f}",
        f"Avg Loss: ₹{avg_l:.2f}",
    ]
    if best:
        lines.append(f"🏆 Best Trade: {best.get('symbol')} {_fmt_p(_safe_float(best.get('pnl_inr')))}")
    if worst:
        lines.append(f"⚠ Worst Trade: {worst.get('symbol')} {_fmt_p(_safe_float(worst.get('pnl_inr')))}")
    lines.append("")
    lines.append("Strategy Performance")
    for k, v in sorted(by_strategy.items(), key=lambda x: x[1], reverse=True)[:5]:
        count = sum(1 for t in trades if str(t.get("strategy_tag") or "") == k)
        lines.append(f"{k} → {_fmt_p(v)} ({count})")
    lines.append("")
    lines.append("Sector Performance")
    for k, v in sorted(by_sector.items(), key=lambda x: x[1], reverse=True)[:5]:
        lines.append(f"{k} → {_fmt_p(v)}")
    lines.append("")
    lines.append("Skipped Signals")
    for k, v in sorted(by_reason.items(), key=lambda x: x[1], reverse=True)[:5]:
        lines.append(f"{k} → {v}")
    if missed:
        lines.append("")
        lines.append("Missed Opportunities")
        for s, side, reason, mv in missed:
            lines.append(f"{s} {side} skipped ({reason}) → {mv:+.2f}%")
    if insights:
        lines.append("")
        lines.append("📌 Insights")
        for i in insights:
            lines.append(f"• {i}")
    lines.append("")
    lines.append("Bot: Trident Trade Bot")
    lines.append("Developed by AK")
    return "\n".join(lines)
