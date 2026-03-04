#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
TS=$(date +%Y%m%d_%H%M%S)
BK="$ROOT/backups/$TS-restart-cmd"
mkdir -p "$BK"

echo "Backing up bot.py..."
cp -a "$ROOT/bot.py" "$BK/bot.py"

echo "Injecting /restart command..."

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text(encoding="utf-8")

if "/restart" in s:
    print("Restart command already exists — skipping")
    raise SystemExit

inject = '''
        elif cmd == "/restart":
            await event.reply("♻️ Restarting bot service...")
            import asyncio, os
            await asyncio.sleep(2)
            os.system("sudo systemctl restart trident")
'''

# Insert after /status if exists
if re.search(r'elif\s+cmd\s*==\s*"/status"', s):
    s = re.sub(r'(elif\s+cmd\s*==\s*"/status".*?\n)', r'\1' + inject + "\n", s, count=1, flags=re.S)
else:
    # fallback insert before end
    s = s.replace("await asyncio.gather(", inject + "\n    await asyncio.gather(")

p.write_text(s, encoding="utf-8")
print("Restart command added")
PY

echo "Compiling..."
./venv/bin/python -m py_compile bot.py

echo "Restarting service..."
sudo systemctl restart trident

echo "DONE ✅ /restart command ready"
