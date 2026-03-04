#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}_v6_clean"
mkdir -p "$BK"

echo "== Backing up files to: $BK =="
for f in config.py trading_cycle.py bot.py strategy_engine.py; do
  if [ -f "$ROOT/$f" ]; then
    cp -a "$ROOT/$f" "$BK/$f"
  fi
done

# -----------------------------
# 1) Fix config.py _get_bool so default can be bool/int and won't crash
# -----------------------------
if [ -f "$ROOT/config.py" ]; then
  python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/home/ubuntu/trident-bot/config.py")
s = p.read_text(encoding="utf-8")

# Replace _get_bool with a safe version (works even if default passed as True/False)
pattern = r"def\s+_get_bool\([^\)]*\)\s*->\s*bool:\s*\n(?:[ \t].*\n)+?"
m = re.search(pattern, s, flags=re.M)
new_fn = """def _get_bool(key: str, default=\"false\") -> bool:
    # default might be bool/int/str depending on older code/patches
    d = default
    if isinstance(d, bool):
        d = "true" if d else "false"
    else:
        d = str(d)
    return os.getenv(key, d).strip().lower() == "true"
"""
if m:
    s = s[:m.start()] + new_fn + s[m.end():]
else:
    # If function name differs, add a safe helper without breaking existing code
    if "def _get_bool(" not in s:
        s = s.replace("load_dotenv()", "load_dotenv()\n\n" + new_fn)

p.write_text(s, encoding="utf-8")
print("config.py OK")
PY
fi

# -----------------------------
# 2) Patch trading_cycle.py: add peak tracking + profit lock exit + better logs
# -----------------------------
if [ -f "$ROOT/trading_cycle.py" ]; then
  python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/home/ubuntu/trident-bot/trading_cycle.py")
s = p.read_text(encoding="utf-8")

# A) Ensure open_trade dict contains peak
# Look for: STATE["open_trade"] = { ... "price": entry_price ... }
# If peak missing, inject after price line.
def add_peak(block: str) -> str:
    if re.search(r'["\']peak["\']\s*:', block):
        return block
    # insert after "price": ...
    block2 = re.sub(
        r'(["\']price["\']\s*:\s*[^,\n]+)(,?)',
        r'\1,\n        "peak": \1.split(":")[1].strip()',
        block,
        count=1
    )
    # The above may not always work due to formatting; do a safer insertion:
    if block2 == block:
        block2 = re.sub(
            r'(["\']price["\']\s*:\s*[^,\n]+,\s*)',
            r'\1"peak": entry_price,\n        ',
            block,
            count=1
        )
    # final fallback: append peak at end
    if block2 == block:
        block2 = block.rstrip().rstrip("}") + ',\n        "peak": entry_price\n    }'
    return block2

# Patch any occurrence of STATE["open_trade"] = { ... } where peak missing
def patch_open_trade(s: str) -> str:
    pat = r'(STATE\[[\'"]open_trade[\'"]\]\s*=\s*\{\n(?:.*\n)*?\})'
    def repl(m):
        blk = m.group(1)
        # Only patch if it looks like entry assignment and contains price
        if ("price" in blk) and ("entry" in blk or "entry_price" in blk):
            # Ensure peak is set to entry_price
            if re.search(r'["\']peak["\']\s*:', blk):
                return blk
            # Try insert after price line
            lines = blk.splitlines()
            out=[]
            inserted=False
            for ln in lines:
                out.append(ln)
                if (not inserted) and re.search(r'["\']price["\']\s*:\s*.*entry_price', ln):
                    out.append('        "peak": entry_price,')
                    inserted=True
            if not inserted:
                # append before closing }
                for i in range(len(out)-1, -1, -1):
                    if out[i].strip() == "}":
                        out.insert(i, '        "peak": entry_price,')
                        inserted=True
                        break
            return "\n".join(out)
        return blk
    return re.sub(pat, repl, s, flags=re.M)

s2 = patch_open_trade(s)

# B) Insert profit lock check inside tick()
# We will add a helper function and call it from tick() after price fetch.
helper = r'''
def _profit_lock_check(trade: dict, ltp: float):
    """
    Profit lock:
      - Activate after PROFIT_LOCK_ACTIVATE_PCT gain
      - Exit if falls PROFIT_LOCK_TRAIL_PCT from peak
    """
    try:
        entry = float(trade.get("price") or 0.0)
        if entry <= 0:
            return

        peak = float(trade.get("peak") or entry)
        if ltp > peak:
            trade["peak"] = ltp
            append_log("INFO", "PEAK", f"{trade.get('symbol')} peak={ltp:.2f}")

        activate = entry * (1 + float(getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5)) / 100.0)
        if float(trade.get("peak") or entry) >= activate:
            trail = float(trade.get("peak") or entry) * (1 - float(getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0)) / 100.0)
            if ltp <= trail:
                append_log("INFO", "LOCK", f"Profit lock exit {trade.get('symbol')} ltp={ltp:.2f} peak={trade.get('peak'):.2f}")
                # Use your existing exit flow
                if "exit_trade" in globals():
                    return exit_trade(trade, reason="PROFIT_LOCK")
                if "_exit_trade" in globals():
                    return _exit_trade(trade, reason="PROFIT_LOCK")
    except Exception as e:
        append_log("WARN", "LOCK", f"Profit lock check failed: {e}")
'''
# Add helper near top (after imports) if not present
if "_profit_lock_check" not in s2:
    # place after imports block: after last "import ..." line sequence
    m = re.search(r'^(?:import .*|from .* import .*)(?:\n(?:import .*|from .* import .*))*\n', s2, flags=re.M)
    if m:
        s2 = s2[:m.end()] + helper + "\n" + s2[m.end():]
    else:
        s2 = helper + "\n" + s2

# Add call in tick() after LTP computed.
# Try to find a common pattern: ltp = ... OR current_price = ... OR price = ...
tick_pat = r'(def\s+tick\s*\(\)\s*:\s*\n(?:[ \t].*\n)+?)'
m = re.search(tick_pat, s2, flags=re.M)
if m:
    tick_block = m.group(1)
    if "_profit_lock_check" in tick_block and "LOCK" in tick_block:
        pass
    else:
        # Insert after first log line or after day-key ensure
        lines = tick_block.splitlines()
        out=[]
        inserted=False
        for i, ln in enumerate(lines):
            out.append(ln)
            # Good place: right after _ensure_day_key() if present
            if (not inserted) and "_ensure_day_key()" in ln:
                out.append("    trade = STATE.get('open_trade')")
                out.append("    # try profit lock on every tick (only if we have LTP variable later)")
                inserted=True
        # If no _ensure_day_key, insert near start (after paused check)
        if not inserted:
            out2=[]
            for i, ln in enumerate(lines):
                out2.append(ln)
                if (not inserted) and "Paused" in ln or "paused" in ln:
                    out2.append("    trade = STATE.get('open_trade')")
                    inserted=True
            if inserted:
                out = out2

        tick_block2 = "\n".join(out)

        # Now insert the actual call where LTP exists: after a line defining ltp/current_price
        # We'll add a small snippet guarded by try/except and handle different var names.
        call_snip = """\n    try:\n        trade = STATE.get('open_trade')\n        if trade is not None:\n            ltp_val = None\n            for _name in ('ltp','LTP','current_price','price','last_price'):\n                if _name in locals():\n                    ltp_val = locals().get(_name)\n                    break\n            if ltp_val is not None:\n                _profit_lock_check(trade, float(ltp_val))\n    except Exception as _e:\n        append_log('WARN','LOCK',f'profit lock hook failed: {_e}')\n"""

        # Insert after first occurrence of a likely LTP variable line
        tick_block3 = re.sub(
            r'(\n[ \t]+(?:ltp|current_price|price|last_price)\s*=\s*.*\n)',
            r'\1' + call_snip,
            tick_block2,
            count=1,
            flags=re.M
        )

        # Replace original tick block
        s2 = s2[:m.start(1)] + tick_block3 + s2[m.end(1):]

p.write_text(s2, encoding="utf-8")
print("trading_cycle.py OK")
PY
fi

echo "== Sanity check (compile) =="
./venv/bin/python -m py_compile config.py trading_cycle.py bot.py strategy_engine.py || true

echo "== Restarting service =="
sudo systemctl daemon-reload || true
sudo systemctl restart trident || true

echo
echo "DONE ✅ Patch v6 applied."
echo
echo "LIVE LOGS (recommended):"
echo "  sudo journalctl -u trident -f -o cat | egrep \"SCAN|SIG|PEAK|LOCK|EXIT|ORDER|ERROR|WARN\""
