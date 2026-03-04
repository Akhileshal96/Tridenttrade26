#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
FILE="$ROOT/bot.py"
BKDIR="$ROOT/backups"

echo "== Stop service to avoid restart loops =="
sudo systemctl stop trident || true

echo "== Restore bot.py from latest *-bot-handler-safe backup (if exists) =="
LATEST="$(ls -td "$BKDIR"/*-bot-handler-safe 2>/dev/null | head -1 || true)"
if [ -n "${LATEST}" ] && [ -f "${LATEST}/bot.py" ]; then
  cp -a "${LATEST}/bot.py" "$FILE"
  echo "✅ Restored bot.py from: ${LATEST}/bot.py"
else
  echo "⚠️ No *-bot-handler-safe backup found. Keeping current bot.py."
fi

TS="$(date +%Y%m%d_%H%M%S)"
SAFE_BK="$BKDIR/${TS}-bot-handler-safe-fixed"
mkdir -p "$SAFE_BK"
cp -a "$FILE" "$SAFE_BK/bot.py"
echo "✅ Backup saved: $SAFE_BK/bot.py"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text(encoding="utf-8", errors="replace")

if "TRIDENT_HANDLER_SAFE_v2" in s:
    print("Already patched (v2) - skipping")
    raise SystemExit(0)

# Find Telethon handler decorator (very common patterns)
m = re.search(r'@.*\.on\([^)]*NewMessage[^)]*\)\s*\n\s*async\s+def\s+\w+\(event\)\s*:\s*\n', s)
if not m:
    # fallback: find any async def handler(event):
    m = re.search(r'\n\s*async\s+def\s+\w+\(event\)\s*:\s*\n', s)
    if not m:
        raise SystemExit("❌ Could not find a Telethon async def ...(event): handler")

insert_pos = m.end()  # IMPORTANT: after the newline, so we insert inside body

patch = (
"    # TRIDENT_HANDLER_SAFE_v2\n"
"    # Safe command block injected automatically\n"
"    try:\n"
"        raw = (event.raw_text or '').strip().lower()\n"
"    except Exception:\n"
"        raw = ''\n"
"\n"
"    if raw == '/excluded':\n"
"        try:\n"
"            if hasattr(CYCLE, 'exclusions_text'):\n"
"                await event.reply(CYCLE.exclusions_text())\n"
"            elif hasattr(CYCLE, 'get_excluded_text'):\n"
"                await event.reply(CYCLE.get_excluded_text())\n"
"            else:\n"
"                await event.reply('⚠️ exclusions view not available')\n"
"        except Exception as e:\n"
"            await event.reply(f'❌ exclusions error: {e}')\n"
"        return\n"
"\n"
"    if raw == '/restart':\n"
"        await event.reply('🔁 Restarting service…')\n"
"        import os\n"
"        os.system('sudo systemctl restart trident &')\n"
"        return\n"
"\n"
)

s = s[:insert_pos] + patch + s[insert_pos:]
p.write_text(s, encoding="utf-8")
print("✅ bot.py patched (v2) OK")
PY

# Compile using venv python if available
PYBIN="$ROOT/venv/bin/python"
if [ ! -x "$PYBIN" ]; then PYBIN="$(command -v python3)"; fi

echo "== Compile sanity =="
"$PYBIN" -m py_compile "$FILE"

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ /excluded and /restart should work now"
