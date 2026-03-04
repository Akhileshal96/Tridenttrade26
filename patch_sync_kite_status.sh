#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
F="$ROOT/trading_cycle.py"
BK="$ROOT/backups/$(date +%Y%m%d_%H%M%S)-sync-kite-status"
mkdir -p "$BK"

if [ ! -f "$F" ]; then
  echo "ERROR: $F not found"
  exit 1
fi

cp -a "$F" "$BK/trading_cycle.py"
echo "Backup: $BK/trading_cycle.py"

python3 - <<'PY'
import re
from pathlib import Path

p = Path("/home/ubuntu/trident-bot/trading_cycle.py")
s = p.read_text(encoding="utf-8")

sync_fn = r'''
def _sync_open_trade_from_kite() -> None:
    """
    If STATE has no open_trade but Kite has a live open position,
    populate STATE["open_trade"] so /status + exits work after restarts.
    """
    try:
        # Only relevant for LIVE mode
        if not is_live_enabled():
            return

        # If already known, don't overwrite
        if STATE.get("open_trade"):
            return

        kite = get_kite()
        pos = kite.positions()
        net = pos.get("net", []) if isinstance(pos, dict) else []

        exch = getattr(CFG, "EXCHANGE", "NSE")

        # Pick the first open position on the configured exchange
        for row in net:
            if str(row.get("exchange", "")).upper() != str(exch).upper():
                continue

            qty = int(row.get("quantity") or 0)
            if qty == 0:
                continue

            symbol = row.get("tradingsymbol")
            avg_price = float(row.get("average_price") or 0.0)

            side = "BUY" if qty > 0 else "SELL"
            abs_qty = abs(qty)

            # Fallback to LTP if avg_price is 0
            if (avg_price <= 0) and symbol:
                try:
                    key = f"{exch}:{symbol}"
                    ltp = kite.ltp(key)[key]["last_price"]
                    avg_price = float(ltp)
                except Exception:
                    pass

            if symbol and avg_price > 0:
                STATE["open_trade"] = {
                    "symbol": symbol,
                    "side": side,
                    "qty": abs_qty,
                    "price": avg_price,
                    "entry_time": "SYNCED_FROM_KITE",
                }
                # Reset trailing/profit-lock peak tracking (if you use it)
                if "peak_pct" in STATE:
                    STATE["peak_pct"] = None
                if "peak" in STATE:
                    STATE["peak"] = None

                append_log("INFO", "SYNC", f"Synced open trade from Kite: {symbol} side={side} qty={abs_qty} avg={avg_price}")
                return

    except Exception as e:
        try:
            STATE["last_error"] = str(e)
        except Exception:
            pass
        append_log("ERROR", "SYNC", f"sync_open_trade_from_kite failed: {e}")
'''.strip("\n")

# 1) Insert sync function if missing
if "def _sync_open_trade_from_kite" not in s:
    # Insert above get_status_text if it exists; else append at end.
    m = re.search(r'^\s*def\s+get_status_text\s*\(', s, flags=re.M)
    if m:
        s = s[:m.start()] + sync_fn + "\n\n" + s[m.start():]
    else:
        s = s.rstrip() + "\n\n" + sync_fn + "\n"

# 2) Ensure get_status_text calls sync
def ensure_call_in_get_status(text: str) -> str:
    m = re.search(r'^\s*def\s+get_status_text\s*\(.*?\):\s*$', text, flags=re.M)
    if not m:
        return text

    # Find first line inside function
    start = m.end()
    # Grab next ~25 lines to patch safely
    block_end = start
    lines = text[start:].splitlines(True)
    head = "".join(lines[:30])
    if "_sync_open_trade_from_kite()" in head:
        return text

    # Prefer inserting right after a line like: n = now_ist() / n = now()
    pat = r'(^\s+.*\bn\s*=\s*now_ist\(\)\s*$|^\s+.*\bn\s*=\s*now\(\)\s*$|^\s+.*\bn\s*=\s*datetime\.now.*$)'
    mm = re.search(pat, head, flags=re.M)
    if mm:
        ins_at = start + mm.end()
        return text[:ins_at] + "\n    _sync_open_trade_from_kite()" + text[ins_at:]

    # Fallback: insert immediately after def line with 4-space indent
    return text[:start] + "\n    _sync_open_trade_from_kite()\n" + text[start:]

s = ensure_call_in_get_status(s)

# 3) Ensure tick() calls sync
def ensure_call_in_tick(text: str) -> str:
    m = re.search(r'^\s*def\s+tick\s*\(.*?\):\s*$', text, flags=re.M)
    if not m:
        return text

    start = m.end()
    lines = text[start:].splitlines(True)
    head = "".join(lines[:80])
    if "_sync_open_trade_from_kite()" in head:
        return text

    # Insert after _ensure_day_rollover() or _ensure_day_key() if present
    mm = re.search(r'^\s+(_ensure_day_rollover\(\)|_ensure_day_key\(\))\s*$', head, flags=re.M)
    if mm:
        ins_at = start + mm.end()
        return text[:ins_at] + "\n    _sync_open_trade_from_kite()" + text[ins_at:]

    # Otherwise insert after first "paused" guard if present
    mm = re.search(r'^\s+if\s+STATE\[[\'"]paused[\'"]\]\s*:\s*$', head, flags=re.M)
    if mm:
        # Put after the return under it (next return line)
        mm2 = re.search(r'^\s+return\s*$', head[mm.end():], flags=re.M)
        if mm2:
            ins_at = start + mm.end() + mm2.end()
            return text[:ins_at] + "\n\n    _sync_open_trade_from_kite()" + text[ins_at:]

    # Fallback: add near top of tick
    return text[:start] + "\n    _sync_open_trade_from_kite()\n" + text[start:]

s = ensure_call_in_tick(s)

p.write_text(s, encoding="utf-8")
print("OK: trading_cycle.py patched for Kite sync + /status visibility")
PY

# Sanity check compile
./venv/bin/python -m py_compile "$F"
echo "OK: python compile passed"

echo ""
echo "Next:"
echo "  sudo systemctl restart trident"
echo "  sudo journalctl -u trident -n 80 -o cat | egrep 'SYNC|STATUS|EXIT|LOCK|ERROR|WARN'"
