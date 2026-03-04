#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/$TS"
mkdir -p "$BK"

echo "[1/6] Backing up files to: $BK"
cp -a "$ROOT/trading_cycle.py" "$BK/trading_cycle.py.bak"
cp -a "$ROOT/strategy_engine.py" "$BK/strategy_engine.py.bak"

echo "[2/6] Patching trading_cycle.py (tick + universe logs)"
python3 - <<'PY'
import io, re, sys, pathlib

path = pathlib.Path("/home/ubuntu/trident-bot/trading_cycle.py")
txt = path.read_text(encoding="utf-8")

# Ensure append_log import exists
if "from log_store import append_log" not in txt:
    # try insert after existing imports
    m = re.search(r"^(import[^\n]*\n)+", txt, flags=re.M)
    if m:
        insert_at = m.end()
        txt = txt[:insert_at] + "from log_store import append_log\n" + txt[insert_at:]
    else:
        txt = "from log_store import append_log\n" + txt

# Find tick() function
m = re.search(r"def\s+tick\s*\(\s*\)\s*:\s*\n", txt)
if not m:
    raise SystemExit("tick() not found in trading_cycle.py")

start = m.end()
# Determine indentation level inside tick()
# Get first line after function header
after = txt[start:]
lines = after.splitlines(True)
if not lines:
    raise SystemExit("tick() body missing")

# Insert logging block right after def tick():
# But avoid duplicating if already patched
marker = 'append_log("INFO", "TICK", "Tick running")'
if marker in txt:
    path.write_text(txt, encoding="utf-8")
    print("trading_cycle.py already patched.")
    sys.exit(0)

indent = None
for ln in lines:
    if ln.strip() == "":
        continue
    indent = re.match(r"^(\s+)", ln).group(1)
    break
if indent is None:
    indent = "    "

inject = (
    f'{indent}append_log("INFO", "TICK", "Tick running")\n'
    f'{indent}if STATE.get("paused"):\n'
    f'{indent}    append_log("INFO", "TICK", "Paused=True (use /startloop)")\n'
    f'{indent}    return\n'
)

# Replace an existing "if STATE['paused']: return" with verbose version if present,
# otherwise just inject at top
# We'll inject at top and also try to remove silent pause return.
txt2 = txt[:start] + inject + txt[start:]

# If there is a silent pause check later, remove it to avoid double-return
txt2 = re.sub(r'\n(\s+)if\s+STATE\["paused"\]\s*:\s*return\s*\n', "\n", txt2)

# After universe loaded, log size if we can locate "uni = load_universe()"
txt2 = re.sub(
    r'(\n\s*uni\s*=\s*load_universe\s*\(\s*\)\s*\n)',
    r'\1    append_log("INFO", "TICK", f"Universe size: {len(uni)}")\n',
    txt2,
    count=1
)

path.write_text(txt2, encoding="utf-8")
print("Patched trading_cycle.py OK")
PY

echo "[3/6] Patching strategy_engine.py (scan logs)"
python3 - <<'PY'
import re, sys, pathlib

path = pathlib.Path("/home/ubuntu/trident-bot/strategy_engine.py")
txt = path.read_text(encoding="utf-8")

# Add import for append_log
if "from log_store import append_log" not in txt:
    # Insert after other imports
    lines = txt.splitlines(True)
    out = []
    inserted = False
    for i, ln in enumerate(lines):
        out.append(ln)
        # Insert after last import line block
        if not inserted and ln.startswith("import") or ln.startswith("from"):
            # lookahead: if next line is not import/from, insert here
            nxt = lines[i+1] if i+1 < len(lines) else ""
            if not (nxt.startswith("import") or nxt.startswith("from")):
                out.append("from log_store import append_log\n")
                inserted = True
    if not inserted:
        out.insert(0, "from log_store import append_log\n")
    txt = "".join(out)

# Avoid duplicate patch
if 'append_log("INFO", "SCAN", f"Scanning {sym}")' in txt:
    path.write_text(txt, encoding="utf-8")
    print("strategy_engine.py already patched.")
    sys.exit(0)

# Insert scan log inside loop: for sym in universe:
# Find that loop
m = re.search(r"\n(\s*)for\s+sym\s+in\s+universe\s*:\s*\n", txt)
if not m:
    raise SystemExit("Loop `for sym in universe:` not found in strategy_engine.py")

indent = m.group(1) + "    "
insert_pos = m.end()

txt = txt[:insert_pos] + f'{indent}append_log("INFO", "SCAN", f"Scanning {sym}")\n' + txt[insert_pos:]

# Add "no signal" log just before returning None (first occurrence)
txt = re.sub(
    r"\n(\s*)return\s+None\s*\n",
    r'\n\1append_log("INFO", "SCAN", "No signal found in universe")\n\1return None\n',
    txt,
    count=1
)

path.write_text(txt, encoding="utf-8")
print("Patched strategy_engine.py OK")
PY

echo "[4/6] Quick syntax check"
cd "$ROOT"
./venv/bin/python -m py_compile trading_cycle.py strategy_engine.py || {
  echo "❌ Syntax check failed. Restoring backups..."
  cp -a "$BK/trading_cycle.py.bak" "$ROOT/trading_cycle.py"
  cp -a "$BK/strategy_engine.py.bak" "$ROOT/strategy_engine.py"
  exit 1
}

echo "[5/6] Restarting service"
sudo systemctl restart trident

echo "[6/6] Showing last 40 journal lines"
sudo journalctl -u trident -n 40 --no-pager

echo "✅ Patch applied successfully."
echo "Now run: /startloop  (Telegram) and then /logs to see TICK + SCAN lines."
