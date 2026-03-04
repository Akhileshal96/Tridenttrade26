#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%F-%H%M%S)"
BK="$ROOT/backups/$TS-orphan-if-fix"
mkdir -p "$BK"
cp -a bot.py "$BK/bot.py"

echo "== Detecting compile error line =="
LINE_NUM="$(
./venv/bin/python - <<'PY'
import py_compile, sys, traceback
try:
    py_compile.compile("bot.py", doraise=True)
    print("OK")
except IndentationError as e:
    # e.lineno is usually set
    print(e.lineno or "0")
except SyntaxError as e:
    print(e.lineno or "0")
except Exception:
    # fallback: try to find "line N" in traceback
    tb = traceback.format_exc()
    import re
    m = re.search(r'line (\d+)', tb)
    print(m.group(1) if m else "0")
PY
)"

if [ "$LINE_NUM" = "OK" ]; then
  echo "✅ bot.py already compiles. Nothing to fix."
  exit 0
fi

if [ "$LINE_NUM" = "0" ]; then
  echo "❌ Could not detect failing line number."
  exit 1
fi

echo "== Fixing orphan block at bot.py line $LINE_NUM =="

python3 - <<PY
from pathlib import Path
import re

ln = int("$LINE_NUM")
p = Path("bot.py")
lines = p.read_text().splitlines(True)

idx = ln - 1
if idx < 0 or idx >= len(lines):
    raise SystemExit(f"Line {ln} out of range")

line = lines[idx]
# Only patch if it looks like a block header ending with :
# (if/elif/for/while/try/except/finally/with/def/class)
hdr = re.match(r'^(\s*)(if|elif|for|while|try|except|finally|with|def|class)\b.*:\s*$', line)
if not hdr:
    raise SystemExit(f"Line {ln} is not a block header: {line.strip()}")

base_indent = len(hdr.group(1))

# Find next non-empty line (or EOF)
j = idx + 1
while j < len(lines) and lines[j].strip() == "":
    j += 1

# Determine next indent (0 if EOF)
next_indent = 0 if j >= len(lines) else (len(lines[j]) - len(lines[j].lstrip(" ")))

# If the next line is not more indented => orphan header: insert pass
if j >= len(lines) or next_indent <= base_indent:
    insert_at = idx + 1
    lines.insert(insert_at, " " * (base_indent + 4) + "pass  # auto-fix orphan block\n")
    p.write_text("".join(lines))
    print(f"✅ Inserted pass after line {ln}")
else:
    print(f"ℹ️ Line {ln} has a valid block body. No change made.")
PY

echo "== Compile sanity (bot.py + trading_cycle.py) =="
./venv/bin/python -m py_compile bot.py trading_cycle.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ fixed orphan indent + restarted"
echo "Backup: $BK"
