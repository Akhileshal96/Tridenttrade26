#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%F-%H%M%S)"
BK="$ROOT/backups/$TS-profit-lock-restore"
mkdir -p "$BK"
cp -a trading_cycle.py "$BK/trading_cycle.py"
cp -a config.py "$BK/config.py" || true

echo "== Patch config defaults (safe) =="
python3 - <<'PY'
from pathlib import Path
p = Path("config.py")
s = p.read_text(encoding="utf-8", errors="replace")

def ensure_line(key, default):
    global s
    if key in s:
        return
    s += f"\n# --- Profit lock defaults (auto-added) ---\n{key} = {default}\n"

ensure_line("PROFIT_LOCK_ACTIVATE_PCT", "1.5")  # activates after +1.5% profit
ensure_line("PROFIT_LOCK_TRAIL_PCT", "2.0")     # trail by 2% from peak pnl%
p.write_text(s, encoding="utf-8")
print("config.py updated (if missing keys)")
PY

echo "== Patch trading_cycle.py =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("trading_cycle.py")
s = p.read_text(encoding="utf-8", errors="replace")

# 1) Ensure STATE has 'peak' key (if not present)
if re.search(r'(?ms)^\s*STATE\s*=\s*\{.*?\}\s*$', s) and '"peak"' not in s:
    s = re.sub(r'(?ms)(^\s*STATE\s*=\s*\{)(.*?)(^\s*\}\s*$)',
               lambda m: m.group(1) + m.group(2) + '    "peak": None,\n' + m.group(3),
               s)

# Helper: find/replace a day reset that wipes peak incorrectly.
# We will change it so peak resets ONLY when no open trade.
# Replace text "reset today_pnl/peak" behaviour: set today_pnl=0 always, peak only if no open trade.
patterns = [
    r'(?m)^\s*STATE\[\s*[\'"]today_pnl[\'"]\s*\]\s*=\s*0\.0\s*$',
]
# We'll inject guard after the today_pnl reset (if exists)
if re.search(patterns[0], s):
    s = re.sub(patterns[0],
               'STATE["today_pnl"] = 0.0\n'
               '    # Do NOT reset peak while a trade is open (profit-lock needs it)\n'
               '    if STATE.get("open_trade") is None:\n'
               '        STATE["peak"] = None',
               s, count=1)

# 2) Ensure check_profit_lock exists
if "def check_profit_lock" not in s:
    # Insert before tick()
    insert_point = re.search(r'(?m)^def\s+tick\s*\(\s*\)\s*:', s)
    if not insert_point:
        raise SystemExit("Could not find tick() to insert profit lock helper")

    helper = '''
# =========================
# Profit lock (trailing from peak pnl%)
# =========================
def check_profit_lock(price: float):
    trade = STATE.get("open_trade")
    if not trade:
        return

    entry = float(trade.get("price", 0) or 0)
    if entry <= 0:
        return

    pnl_pct = (price - entry) / entry * 100.0

    # peak tracks max pnl% since entry (do not reset mid-trade)
    if STATE.get("peak") is None:
        STATE["peak"] = pnl_pct
    else:
        STATE["peak"] = max(float(STATE["peak"]), pnl_pct)

    activate = float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5))
    trail_pct = float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0))

    if STATE["peak"] >= activate:
        trail = STATE["peak"] - trail_pct
        if pnl_pct <= trail:
            exit_trade("Profit lock trail")
'''
    s = s[:insert_point.start()] + helper + "\n" + s[insert_point.start():]

# 3) Ensure tick() calls profit lock when open trade exists
# We'll add `check_profit_lock(ltp)` after fetching ltp.
tick_block = re.search(r'(?ms)^def\s+tick\s*\(\s*\)\s*:(.*?)(?=^\S)', s)
if not tick_block:
    # if tick is last in file, grab till end
    tick_block = re.search(r'(?ms)^def\s+tick\s*\(\s*\)\s*:(.*)$', s)
if not tick_block:
    raise SystemExit("Could not parse tick()")

tick_text = tick_block.group(0)

# common pattern: ltp = kite.ltp(...)[...]['last_price']
# add check_profit_lock(ltp) if missing
if "check_profit_lock(" not in tick_text:
    # Insert after first occurrence of ltp assignment
    tick_text2 = re.sub(
        r'(?m)^(.*\bltp\s*=\s*.*last_price.*)$',
        r'\1\n        check_profit_lock(ltp)',
        tick_text,
        count=1
    )
    if tick_text2 == tick_text:
        # fallback: insert after any ltp assignment line
        tick_text2 = re.sub(
            r'(?m)^(.*\bltp\s*=\s*.*)$',
            r'\1\n        check_profit_lock(ltp)',
            tick_text,
            count=1
        )
    s = s.replace(tick_text, tick_text2, 1)

# 4) Improve status text if get_status_text exists
if "def get_status_text" in s:
    s = re.sub(
        r'(?m)^(.*Profit lock:.*)$',
        r'\1',
        s
    )
    # If profit lock line missing, add near open trade info
    if "Profit lock:" not in s:
        s = re.sub(
            r'(?m)^(.*Open trade:.*)$',
            r'\1\n    lines.append(f"🔒 Profit lock: peak={STATE.get(\'peak\') if STATE.get(\'peak\') is not None else \'NA\'} | activates at {getattr(CFG, \'PROFIT_LOCK_ACTIVATE_PCT\', 1.5):.2f}% | trail {getattr(CFG, \'PROFIT_LOCK_TRAIL_PCT\', 2.0):.2f}%")',
            s,
            count=1
        )

p.write_text(s, encoding="utf-8")
print("trading_cycle.py patched")
PY

echo "== Compile sanity =="
./venv/bin/python -m py_compile trading_cycle.py config.py

echo "== Restart =="
sudo systemctl restart trident

echo "DONE ✅ Profit lock restored + peak reset fixed"
echo "Backup: $BK"
