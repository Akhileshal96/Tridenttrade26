#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

echo "== Backup bot.py =="
cp bot.py backups/bot.py.syntaxfix.$(date +%F-%H%M%S)

echo "== Removing broken arrow fragments =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("bot.py")
s = p.read_text()

# remove stray '-> list' or arrow fragments
s = re.sub(r'^\s*->\s*list\s*$', '', s, flags=re.M)

# remove half-injected restart handlers
s = re.sub(r'elif\s+cmd\s*==\s*"/restart"\s*:\s*$', '', s, flags=re.M)

p.write_text(s)
print("bot.py cleaned")
PY

echo "== Compile sanity =="
./venv/bin/python -m py_compile bot.py trading_cycle.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅"
