#!/bin/bash
set -e

FILE="trading_cycle.py"
cp $FILE ${FILE}.bak

python3 - <<'PY'
from pathlib import Path
p = Path("trading_cycle.py")
code = p.read_text()

# ---------- Add load_universe() if missing ----------
if "def load_universe():" not in code:
    load_func = '''

def load_universe():
    # Prefer config universe
    uni = getattr(CFG, "UNIVERSE", None)
    if isinstance(uni, (list, tuple)) and uni:
        return [str(x).strip().upper() for x in uni if str(x).strip()]

    # Fallback to universe file
    path = getattr(CFG, "UNIVERSE_PATH", "/home/ubuntu/trident-bot/data/universe.txt")
    try:
        with open(path, "r") as f:
            return [l.strip().upper() for l in f.read().splitlines() if l.strip()]
    except Exception:
        return []
'''
    code = load_func + code

# ---------- Fix generate_signal call ----------
code = code.replace(
    "sig = generate_signal(universe)",
    """universe = load_universe()
    if not universe:
        append_log("WARN","SCAN","Universe empty — waiting for night research")
        return

    append_log("INFO","SCAN",f"Scanning {len(universe)} symbols")
    sig = generate_signal(universe)"""
)

p.write_text(code)
PY

echo "Universe patch applied"
