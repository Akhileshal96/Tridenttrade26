#!/bin/bash
set -e

FILE="trading_cycle.py"
cp $FILE ${FILE}.bak

python3 - <<'PY'
from pathlib import Path
p = Path("trading_cycle.py")
code = p.read_text()

# Add sync + scan imports if missing
if "from strategy_engine import generate_signal" not in code:
    code = "from strategy_engine import generate_signal\n" + code

# Add scan block inside tick
scan_block = """
    # ===== SCAN UNIVERSE =====
    if not STATE.get("open_trade"):
        try:
            uni = load_universe()
            append_log("INFO","SCAN",f"Scanning {len(uni)} symbols")

            sig = generate_signal(uni)
            if sig:
                append_log("INFO","SIGNAL",f"{sig['symbol']} {sig['side']} {sig['entry']}")
                STATE["open_trade"] = sig

        except Exception as e:
            append_log("ERROR","SCAN",str(e))
"""

if "Scanning" not in code:
    code = code.replace("append_log(\"INFO\",\"TICK\",\"Tick running\")",
                        "append_log(\"INFO\",\"TICK\",\"Tick running\")\n" + scan_block)

p.write_text(code)
PY

echo "Patch applied"
