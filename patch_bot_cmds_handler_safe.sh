#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
FILE="$ROOT/bot.py"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-bot-handler-safe"
mkdir -p "$BK"
cp -a "$FILE" "$BK/bot.py"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text(encoding="utf-8", errors="replace")

if "TRIDENT_HANDLER_SAFE_v1" in s:
    print("Already patched")
    raise SystemExit

# Find Telethon message handler
m = re.search(r'@.*\.on\([^)]*NewMessage', s)
if not m:
    raise SystemExit("❌ Could not find Telethon handler")

# Find function definition after handler
func = re.search(r'async\s+def\s+\w+\(event\):', s[m.start():])
if not func:
    raise SystemExit("❌ Could not find handler function")

insert_pos = m.start() + func.end()

patch = """

    # TRIDENT_HANDLER_SAFE_v1
    try:
        raw = event.raw_text.strip().lower()
    except:
        raw = ""

    if raw == "/excluded":
        try:
            if hasattr(CYCLE, "exclusions_text"):
                await event.reply(CYCLE.exclusions_text())
            else:
                await event.reply("⚠️ exclusions not available")
        except Exception as e:
            await event.reply(f"❌ exclusions error: {e}")
        return

    if raw == "/restart":
        await event.reply("🔁 Restarting service…")
        import os
        os.system("sudo systemctl restart trident &")
        return

"""

s = s[:insert_pos] + patch + s[insert_pos:]
p.write_text(s, encoding="utf-8")
print("bot.py handler patch applied")
PY

PY="$ROOT/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
"$PY" -m py_compile "$FILE"

sudo systemctl restart trident
echo "DONE ✅"
