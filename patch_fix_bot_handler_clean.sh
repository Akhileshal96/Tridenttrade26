#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

echo "== Stop service (avoid restart loops) =="
sudo systemctl stop trident || true

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-bot-handler-clean"
mkdir -p "$BK"
cp -a "$ROOT/bot.py" "$BK/bot.py"
echo "Backup: $BK/bot.py"

python3 - <<'PY'
from pathlib import Path

p = Path("/home/ubuntu/trident-bot/bot.py")
lines = p.read_text(encoding="utf-8", errors="replace").splitlines(True)

# Find decorator + handler signature
dec_i = None
def_i = None
for i, ln in enumerate(lines):
    if "@client.on" in ln and "NewMessage" in ln:
        dec_i = i
        # find next async def handler(event):
        for j in range(i+1, min(i+50, len(lines))):
            if lines[j].lstrip().startswith("async def ") and "handler" in lines[j]:
                def_i = j
                break
        break

if dec_i is None or def_i is None:
    raise SystemExit("❌ Could not find Telethon @client.on(NewMessage) handler block in bot.py")

# Determine function body range by indentation
def_line = lines[def_i]
base_indent = len(def_line) - len(def_line.lstrip(" "))
body_start = def_i + 1

# body ends when indentation drops back to <= base_indent (and line not blank)
end = body_start
for k in range(body_start, len(lines)):
    ln = lines[k]
    if ln.strip() == "":
        end = k + 1
        continue
    ind = len(ln) - len(ln.lstrip(" "))
    if ind <= base_indent and not ln.lstrip().startswith("#"):
        end = k
        break
    end = k + 1

# Fresh handler body (4-space indentation)
handler_body = [
    "    # TRIDENT_HANDLER_CLEAN_v1 (auto-rewritten)\n",
    "    # Safe command router: fixes broken indentation from earlier patches.\n",
    "    try:\n",
    "        text = (event.raw_text or \"\").strip()\n",
    "    except Exception:\n",
    "        text = \"\"\n",
    "    cmd = (text.split()[0].lower() if text else \"\")\n",
    "\n",
    "    # Prefer private messages only (if helper exists)\n",
    "    try:\n",
    "        if ' _is_private' in globals() and callable(globals().get('_is_private')):\n",
    "            if not _is_private(event):\n",
    "                return\n",
    "    except Exception:\n",
    "        pass\n",
    "\n",
    "    # Sender id\n",
    "    try:\n",
    "        sender = int(getattr(event, 'sender_id', 0) or 0)\n",
    "    except Exception:\n",
    "        sender = 0\n",
    "\n",
    "    # Always allow /myid\n",
    "    if cmd == \"/myid\":\n",
    "        await event.reply(f\"🆔 Your Telegram ID: `{sender}`\")\n",
    "        return\n",
    "\n",
    "    # Viewer gate (if helper exists)\n",
    "    try:\n",
    "        if '_is_viewer' in globals() and callable(globals().get('_is_viewer')):\n",
    "            if not _is_viewer(sender):\n",
    "                return\n",
    "    except Exception:\n",
    "        pass\n",
    "\n",
    "    # /status\n",
    "    if cmd == \"/status\":\n",
    "        try:\n",
    "            if 'CYCLE' in globals() and hasattr(CYCLE, 'get_status_text'):\n",
    "                await event.reply(CYCLE.get_status_text())\n",
    "            elif 'CYCLE' in globals() and hasattr(CYCLE, 'status_text'):\n",
    "                await event.reply(CYCLE.status_text())\n",
    "            else:\n",
    "                await event.reply(\"⚠️ Status not available (CYCLE missing)\")\n",
    "        except Exception as e:\n",
    "            await event.reply(f\"❌ status error: {e}\")\n",
    "        return\n",
    "\n",
    "    # /excluded\n",
    "    if cmd == \"/excluded\":\n",
    "        try:\n",
    "            if 'CYCLE' in globals() and hasattr(CYCLE, 'exclusions_text'):\n",
    "                await event.reply(CYCLE.exclusions_text())\n",
    "            elif 'CYCLE' in globals() and hasattr(CYCLE, 'get_excluded_text'):\n",
    "                await event.reply(CYCLE.get_excluded_text())\n",
    "            else:\n",
    "                await event.reply(\"⚠️ Exclusions view not available (trading_cycle missing helper)\")\n",
    "        except Exception as e:\n",
    "            await event.reply(f\"❌ exclusions error: {e}\")\n",
    "        return\n",
    "\n",
    "    # /restart (owner only if helper exists)\n",
    "    if cmd == \"/restart\":\n",
    "        try:\n",
    "            if '_is_owner' in globals() and callable(globals().get('_is_owner')):\n",
    "                if not _is_owner(sender):\n",
    "                    await event.reply(\"⛔ Not allowed\")\n",
    "                    return\n",
    "        except Exception:\n",
    "            pass\n",
    "        await event.reply(\"🔁 Restarting bot service...\")\n",
    "        import os\n",
    "        os.system(\"sudo systemctl restart trident >/dev/null 2>&1 &\")\n",
    "        return\n",
]

new_lines = lines[:dec_i] + lines[dec_i:def_i+1] + handler_body + lines[end:]
p.write_text("".join(new_lines), encoding="utf-8")
print(f"✅ Rewrote handler block: decorator@{dec_i+1}, def@{def_i+1}, replaced body lines {body_start+1}-{end}")
PY

echo "== Compile sanity =="
PYBIN=""
for cand in "$ROOT/venv/bin/python" "$ROOT/.venv/bin/python" "$(command -v python3)"; do
  if [ -x "$cand" ]; then PYBIN="$cand"; break; fi
done
if [ -z "$PYBIN" ]; then echo "❌ python not found"; exit 1; fi
echo "Using: $PYBIN"
"$PYBIN" -m py_compile bot.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ Handler fixed + /excluded + /restart added safely"
