#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
TS=$(date +%Y%m%d_%H%M%S)
BK="$ROOT/backups/$TS-restart-fix"
mkdir -p "$BK"

echo "Backup bot.py"
cp -a "$ROOT/bot.py" "$BK/bot.py"

python3 - <<'PY'
from pathlib import Path

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text()

# remove broken restart if exists
s = s.replace('elif cmd == "/restart":', '# broken restart removed')

# SAFE restart handler block
restart_block = '''
    if text.strip() == "/restart":
        await event.reply("♻️ Restarting bot...")
        import asyncio, os
        await asyncio.sleep(1)
        os.system("sudo systemctl restart trident")
        return
'''

# insert after text extraction
if "text = event.raw_text" in s and "/restart" not in s:
    s = s.replace(
        "text = event.raw_text",
        "text = event.raw_text\n" + restart_block
    )

p.write_text(s)
print("Restart fix applied")
PY

echo "Compile"
./venv/bin/python -m py_compile bot.py

echo "Restart service"
sudo systemctl restart trident

echo "DONE ✅"
