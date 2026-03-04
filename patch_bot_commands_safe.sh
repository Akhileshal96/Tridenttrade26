#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
FILE="$ROOT/bot.py"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-bot-cmds-safe"
mkdir -p "$BK"
cp -a "$FILE" "$BK/bot.py"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text(encoding="utf-8", errors="replace")

# If we've already applied, do nothing
if "TRIDENT_SAFE_CMD_BLOCK_v1" in s:
    print("Safe command block already present - skipping")
    raise SystemExit(0)

lines = s.splitlines(True)

# Find a good insertion point: immediately after first "text = event.raw_text" (or similar)
idx = None
for i, ln in enumerate(lines):
    if re.search(r'\btext\s*=\s*event\.raw_text\b', ln) or re.search(r'\btext\s*=\s*event\.raw_text\.strip\(\)\b', ln):
        idx = i
        break

if idx is None:
    # fallback: after first "text =" assignment
    for i, ln in enumerate(lines):
        if re.search(r'^\s*text\s*=', ln):
            idx = i
            break

if idx is None:
    raise SystemExit("❌ Could not find a safe insertion point (no 'text = ...' line).")

# Determine indentation of the "text =" line
m = re.match(r'^(\s*)', lines[idx])
base = m.group(1) if m else ""
# We'll insert a block at the same indentation level as "text =" (inside handler)
ins = []
ins.append(f"{base}# TRIDENT_SAFE_CMD_BLOCK_v1 (auto-added)\n")
ins.append(f"{base}cmd = (text or '').strip().split()[0].lower() if (text or '').strip() else ''\n")
ins.append(f"{base}if cmd == '/excluded':\n")
ins.append(f"{base}    try:\n")
ins.append(f"{base}        # Prefer exclusions_text() if available\n")
ins.append(f"{base}        if hasattr(CYCLE, 'exclusions_text'):\n")
ins.append(f"{base}            await event.reply(CYCLE.exclusions_text())\n")
ins.append(f"{base}        elif hasattr(CYCLE, 'get_excluded_text'):\n")
ins.append(f"{base}            await event.reply(CYCLE.get_excluded_text())\n")
ins.append(f"{base}        else:\n")
ins.append(f"{base}            await event.reply('⚠️ Exclusions view not available in trading_cycle.py')\n")
ins.append(f"{base}    except Exception as e:\n")
ins.append(f"{base}        await event.reply(f'❌ /excluded failed: {e}')\n")
ins.append(f"{base}    return\n\n")

ins.append(f"{base}if cmd == '/restart':\n")
ins.append(f"{base}    await event.reply('🔁 Restart requested. Attempting service restart…')\n")
ins.append(f"{base}    try:\n")
ins.append(f"{base}        # Create restart flag (always)\n")
ins.append(f"{base}        import os\n")
ins.append(f"{base}        flag_path = getattr(CFG, 'RESTART_FLAG_PATH', '/home/ubuntu/trident-bot/RESTART_REQUIRED')\n")
ins.append(f"{base}        with open(flag_path, 'w', encoding='utf-8') as f:\n")
ins.append(f"{base}            f.write('restart\\n')\n")
ins.append(f"{base}    except Exception:\n")
ins.append(f"{base}        pass\n")
ins.append(f"{base}    try:\n")
ins.append(f"{base}        import os\n")
ins.append(f"{base}        # This works only if sudoers allows it; otherwise user can restart manually.\n")
ins.append(f"{base}        os.system('sudo systemctl restart trident >/dev/null 2>&1 &')\n")
ins.append(f"{base}        await event.reply('✅ Restart command issued. If it does not restart, run: sudo systemctl restart trident')\n")
ins.append(f"{base}    except Exception as e:\n")
ins.append(f"{base}        await event.reply(f'⚠️ Could not auto-restart: {e}\\nRun: sudo systemctl restart trident')\n")
ins.append(f"{base}    return\n\n")

# Insert right after the "text =" line
out = lines[:idx+1] + ins + lines[idx+1:]
p.write_text("".join(out), encoding="utf-8")
print("bot.py patched: /excluded + /restart added safely")
PY

# Compile sanity (use venv python if present)
PY="$ROOT/venv/bin/python"
if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi
"$PY" -m py_compile "$FILE"

sudo systemctl restart trident
echo "DONE ✅ Safe /excluded + /restart patch applied. Backup: $BK"
