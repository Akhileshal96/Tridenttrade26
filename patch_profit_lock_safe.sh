#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-profitlock-safe"
mkdir -p "$BK"
cp -a "$ROOT/trading_cycle.py" "$BK/trading_cycle.py"
cp -a "$ROOT/config.py" "$BK/config.py"
echo "Backup saved to: $BK"

# -------- Patch config.py (safe add if missing) ----------
python3 - <<'PY'
from pathlib import Path

p = Path("/home/ubuntu/trident-bot/config.py")
s = p.read_text(encoding="utf-8", errors="replace")

def ensure(name, value, comment=""):
    global s
    if f"{name} =" in s or f"{name}=" in s:
        return
    line = f"\n# Auto-added defaults (profit lock)\n{name} = {value}"
    if comment:
        line += f"  # {comment}"
    line += "\n"
    s += line

ensure("PROFIT_LOCK_ACTIVATE_PCT", "1.5", "activate after +1.5% PnL%")
ensure("PROFIT_LOCK_TRAIL_PCT", "2.0", "exit if PnL% drops (peak - 2.0)")
p.write_text(s, encoding="utf-8")
print("config.py OK")
PY

# -------- Patch trading_cycle.py safely ----------
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/trading_cycle.py")
s = p.read_text(encoding="utf-8", errors="replace")

# 1) Ensure STATE has "peak"
m = re.search(r'(?ms)^\s*STATE\s*=\s*\{.*?\n\}\s*$', s)
if m:
    block = m.group(0)
    if re.search(r'["\']peak["\']\s*:', block) is None:
        # insert before closing }
        block2 = re.sub(r'(?m)^\}\s*$', '    "peak": None,\n}', block)
        s = s[:m.start()] + block2 + s[m.end():]
else:
    # If no STATE block found, add a minimal one near top after imports
    ins_at = 0
    mi = re.search(r'(?m)^import\s+config\s+as\s+CFG\s*$', s)
    if mi:
        ins_at = mi.end()
    state_block = """
# --------------------------
# Runtime STATE (safe defaults)
# --------------------------
STATE = {
    "paused": True,
    "open_trade": None,
    "today_pnl": 0.0,
    "peak": None,
}
"""
    s = s[:ins_at] + state_block + s[ins_at:]

# 2) Add check_profit_lock if missing
if "def check_profit_lock(" not in s:
    helper = """
# ==========================
# Profit lock (trailing on peak PnL%)
# ==========================
def check_profit_lock(price: float):
    trade = STATE.get("open_trade")
    if not trade:
        return

    try:
        entry = float(trade.get("price") or trade.get("entry") or 0.0)
        if entry <= 0:
            return
        pnl_pct = (float(price) - entry) / entry * 100.0

        if STATE.get("peak") is None:
            STATE["peak"] = pnl_pct

        # update peak
        if pnl_pct > float(STATE["peak"]):
            STATE["peak"] = pnl_pct

        activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
        trail = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))

        # once peak >= activate, exit if pnl drops below (peak - trail)
        if float(STATE["peak"]) >= activate:
            floor = float(STATE["peak"]) - trail
            if pnl_pct <= floor:
                exit_trade(f"Profit lock: peak={STATE['peak']:.2f}% pnl={pnl_pct:.2f}%")
    except Exception as e:
        # keep loop alive, log if logger exists
        try:
            append_log("ERROR", "LOCK", f"profit-lock error: {e}")
        except Exception:
            pass
"""
    s += "\n" + helper

# 3) Ensure universe is never None (fix NoneType iterable)
# Replace: universe = something()  -> if universe is None: universe = []
# We'll inject guard after first assignment we find.
if "if universe is None:" not in s:
    # Look for a line that assigns universe =
    um = re.search(r'(?m)^\s*universe\s*=\s*.+$', s)
    if um:
        line = um.group(0)
        indent = re.match(r'^\s*', line).group(0)
        guard = f"\n{indent}if universe is None:\n{indent}    universe = []\n"
        insert_at = um.end()
        s = s[:insert_at] + guard + s[insert_at:]

# 4) Call check_profit_lock during open trade after LTP fetch
# Try to find open-trade block where LTP is computed.
if "check_profit_lock(" in s:
    # If already called, skip
    pass
else:
    # Find an LTP line inside tick() like: ltp = kite.ltp(...)[...]['last_price']
    lm = re.search(r'(?m)^\s*ltp\s*=\s*.+kite\.ltp\(.+\).+$', s)
    if lm:
        indent = re.match(r'^\s*', lm.group(0)).group(0)
        call = f"\n{indent}check_profit_lock(ltp)\n"
        s = s[:lm.end()] + call + s[lm.end():]

# 5) Day reset should NOT wipe peak if a trade is open (profit lock needs peak)
# Replace simple `STATE["peak"] = None` reset if it exists in day reset section.
s = re.sub(
    r'(?m)^\s*STATE\[\s*["\']peak["\']\s*\]\s*=\s*None\s*$',
    '    # Do NOT reset peak while a trade is open (profit-lock needs it)\n    if STATE.get("open_trade") is None:\n        STATE["peak"] = None',
    s,
    count=1
)

p.write_text(s, encoding="utf-8")
print("trading_cycle.py OK")
PY

# -------- Compile sanity + restart ----------
PY=""
for cand in \
  "$ROOT/venv/bin/python" \
  "$ROOT/.venv/bin/python" \
  "$ROOT/env/bin/python" \
  "$(command -v python3)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done

echo "Using python: $PY"
"$PY" -m py_compile trading_cycle.py
"$PY" -m py_compile config.py

sudo systemctl restart trident
echo "DONE ✅ Profit lock patch applied and service restarted."
