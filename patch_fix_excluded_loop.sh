#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-excluded-loop-fix"
mkdir -p "$BK"
cp bot.py "$BK/bot.py"

echo "Backup saved to: $BK"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("bot.py")
s = p.read_text()

# remove wrongly injected loop call
s = re.sub(r'\s*await event\.reply\(CYCLE\.exclusions_text\(\)\)\s*', '\n', s)

p.write_text(s)
print("excluded loop cleaned")
PY

# compile
./venv/bin/python -m py_compile bot.py || true

sudo systemctl restart trident
echo "DONE ✅ exclusions spam fixed"
