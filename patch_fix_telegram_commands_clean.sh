#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-handler-clean-rewrite"
mkdir -p "$BK"

echo "== Stop service (avoid restart loops) =="
sudo systemctl stop trident || true

echo "== Backup bot.py =="
cp -a "$ROOT/bot.py" "$BK/bot.py"

echo "== Normalize tabs -> spaces (prevents unindent errors) =="
expand -t 4 "$ROOT/bot.py" > "$ROOT/bot.py.__tmp__" && mv "$ROOT/bot.py.__tmp__" "$ROOT/bot.py"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
lines = p.read_text(encoding="utf-8", errors="replace").splitlines(True)

# 1) find decorator line and handler def line
dec_i = None
def_i = None

for i, ln in enumerate(lines):
    if "@client.on" in ln and "NewMessage" in ln:
        # next async def handler(...)
        for j in range(i+1, min(i+80, len(lines))):
            if re.match(r'^\s*async\s+def\s+handler\s*\(\s*event\s*\)\s*:\s*$', lines[j]):
                dec_i = i
                def_i = j
                break
        if def_i is not None:
            break

if def_i is None:
    raise SystemExit("❌ Could not find async def handler(event): after @client.on(NewMessage) in bot.py")

# 2) determine handler body range by indentation
def_line = lines[def_i]
base_indent = len(def_line) - len(def_line.lstrip(" "))
body_start = def_i + 1

# body ends when indentation drops back to <= base_indent (and line not blank/comment)
end = body_start
for k in range(body_start, len(lines)):
    ln = lines[k]
    if ln.strip() == "" or ln.lstrip().startswith("#"):
        continue
    ind = len(ln) - len(ln.lstrip(" "))
    if ind <= base_indent:
        end = k
        break
else:
    end = len(lines)

indent = " " * (base_indent + 4)

new_body = []
new_body.append(f"{indent}# TRIDENT_HANDLER_CLEAN_v1 (auto-rewritten)\n")
new_body.append(f"{indent}# Safe command router. Fixes broken indentation from earlier patches.\n")
new_body.append(f"{indent}try:\n")
new_body.append(f"{indent}    raw_text = (event.raw_text or '').strip()\n")
new_body.append(f"{indent}except Exception:\n")
new_body.append(f"{indent}    raw_text = ''\n")
new_body.append(f"{indent}cmd = raw_text.split()[0].lower() if raw_text else ''\n")
new_body.append(f"{indent}sender = getattr(event, 'sender_id', None)\n")
new_body.append(f"{indent}append_log('INFO','CMD', f\"rx cmd={{cmd}} from={{sender}}\")\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Optional: only respond in private chat\n")
new_body.append(f"{indent}try:\n")
new_body.append(f"{indent}    if hasattr(event, 'is_private') and (not event.is_private):\n")
new_body.append(f"{indent}        return\n")
new_body.append(f"{indent}except Exception:\n")
new_body.append(f"{indent}    pass\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Always allow /myid\n")
new_body.append(f"{indent}if cmd == '/myid':\n")
new_body.append(f"{indent}    await event.reply(f\"🆔 Your Telegram ID: {{sender}}\")\n")
new_body.append(f"{indent}    return\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Help / commands\n")
new_body.append(f"{indent}if cmd in ('/help','/commands'):\n")
new_body.append(f"{indent}    await event.reply(HELP_TEXT)\n")
new_body.append(f"{indent}    return\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Status\n")
new_body.append(f"{indent}if cmd == '/status':\n")
new_body.append(f"{indent}    try:\n")
new_body.append(f"{indent}        await event.reply(CYCLE.get_status_text())\n")
new_body.append(f"{indent}    except Exception as e:\n")
new_body.append(f"{indent}        await event.reply(f\"❌ status error: {{e}}\")\n")
new_body.append(f"{indent}    return\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Excluded list\n")
new_body.append(f"{indent}if cmd == '/excluded':\n")
new_body.append(f"{indent}    try:\n")
new_body.append(f"{indent}        if hasattr(CYCLE, 'exclusions_text'):\n")
new_body.append(f"{indent}            txt = CYCLE.exclusions_text()\n")
new_body.append(f"{indent}        elif hasattr(CYCLE, 'get_excluded_text'):\n")
new_body.append(f"{indent}            txt = CYCLE.get_excluded_text()\n")
new_body.append(f"{indent}        else:\n")
new_body.append(f"{indent}            txt = '⚠️ excluded view not available (missing in trading_cycle.py)'\n")
new_body.append(f"{indent}        await event.reply(txt)\n")
new_body.append(f"{indent}    except Exception as e:\n")
new_body.append(f"{indent}        await event.reply(f\"❌ excluded error: {{e}}\")\n")
new_body.append(f"{indent}    return\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Restart service (manual)\n")
new_body.append(f"{indent}if cmd in ('/restart','/start'):\n")
new_body.append(f"{indent}    await event.reply('🔁 Restarting Trident service...')\n")
new_body.append(f"{indent}    try:\n")
new_body.append(f"{indent}        import subprocess\n")
new_body.append(f"{indent}        subprocess.Popen(['sudo','systemctl','restart','trident'])\n")
new_body.append(f"{indent}    except Exception as e:\n")
new_body.append(f"{indent}        await event.reply(f\"❌ restart failed: {{e}}\")\n")
new_body.append(f"{indent}    return\n")
new_body.append(f"{indent}\n")
new_body.append(f"{indent}# Fallback: ignore\n")
new_body.append(f"{indent}return\n")

# Replace handler body
new_lines = lines[:body_start] + new_body + lines[end:]
p.write_text("".join(new_lines), encoding="utf-8")
print(f"✅ handler rewritten: def line {def_i+1}, body {body_start+1}-{end}")
PY

echo "== Compile sanity =="
PYBIN=""
for cand in "$ROOT/venv/bin/python" "$ROOT/.venv/bin/python" "$ROOT/env/bin/python" "$(command -v python3)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PYBIN="$cand"; break; fi
done
echo "Using: $PYBIN"
"$PYBIN" -m py_compile "$ROOT/bot.py"

echo "== Start service =="
sudo systemctl start trident

echo "✅ DONE: Telegram handler cleaned + /excluded + /restart + command logging"
echo "Backup at: $BK"
