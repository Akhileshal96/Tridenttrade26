#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

echo "== Stop service (avoid restart loops) =="
sudo systemctl stop trident || true

TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-handler-indent-auto"
mkdir -p "$BK"
cp -a "$ROOT/bot.py" "$BK/bot.py"
echo "Backup: $BK/bot.py"

python3 - <<'PY'
from pathlib import Path

p = Path("/home/ubuntu/trident-bot/bot.py")
lines = p.read_text(encoding="utf-8", errors="replace").splitlines(True)

# Find @client.on(NewMessage) and the next async def handler(event):
dec_i = None
def_i = None
for i, ln in enumerate(lines):
    if "@client.on" in ln and "NewMessage" in ln:
        dec_i = i
        for j in range(i + 1, min(i + 80, len(lines))):
            if lines[j].lstrip().startswith("async def ") and "handler" in lines[j]:
                def_i = j
                break
        break

if dec_i is None or def_i is None:
    raise SystemExit("❌ Could not find Telethon @client.on(NewMessage) handler in bot.py")

def_line = lines[def_i]
def_indent = def_line[:len(def_line) - len(def_line.lstrip())]   # exact whitespace prefix
body_indent = def_indent + "    "  # one level deeper than def

# Find end of handler function block
base_indent_len = len(def_indent)
body_start = def_i + 1
end = body_start

for k in range(body_start, len(lines)):
    ln = lines[k]
    if ln.strip() == "":
        end = k + 1
        continue
    ind_len = len(ln) - len(ln.lstrip())
    # when we come back to same or less indentation -> function ends
    if ind_len <= base_indent_len and not ln.lstrip().startswith("#"):
        end = k
        break
    end = k + 1

# Fresh handler body using correct indent level
hb = []
def add(s): hb.append(body_indent + s + "\n")

add("# TRIDENT_HANDLER_CLEAN_v2 (auto-rewritten, indent-safe)")
add("# Safe command router: fixes broken indentation from earlier patches.")
add("try:")
hb.append(body_indent + "    text = (event.raw_text or \"\").strip()\n")
add("except Exception:")
hb.append(body_indent + "    text = \"\"\n")
add("cmd = (text.split()[0].lower() if text else \"\")")
hb.append(body_indent + "\n")

add("# Prefer private messages only (if helper exists)")
add("try:")
hb.append(body_indent + "    if '_is_private' in globals() and callable(globals().get('_is_private')):\n")
hb.append(body_indent + "        if not _is_private(event):\n")
hb.append(body_indent + "            return\n")
add("except Exception:")
hb.append(body_indent + "    pass\n")
hb.append(body_indent + "\n")

add("# Sender id")
add("try:")
hb.append(body_indent + "    sender = int(getattr(event, 'sender_id', 0) or 0)\n")
add("except Exception:")
hb.append(body_indent + "    sender = 0\n")
hb.append(body_indent + "\n")

add("# Always allow /myid")
add("if cmd == \"/myid\":")
hb.append(body_indent + "    await event.reply(f\"🆔 Your Telegram ID: `{sender}`\")\n")
hb.append(body_indent + "    return\n")
hb.append(body_indent + "\n")

add("# Viewer gate (if helper exists)")
add("try:")
hb.append(body_indent + "    if '_is_viewer' in globals() and callable(globals().get('_is_viewer')):\n")
hb.append(body_indent + "        if not _is_viewer(sender):\n")
hb.append(body_indent + "            return\n")
add("except Exception:")
hb.append(body_indent + "    pass\n")
hb.append(body_indent + "\n")

add("# /status")
add("if cmd == \"/status\":")
hb.append(body_indent + "    try:\n")
hb.append(body_indent + "        if 'CYCLE' in globals() and hasattr(CYCLE, 'get_status_text'):\n")
hb.append(body_indent + "            await event.reply(CYCLE.get_status_text())\n")
hb.append(body_indent + "        elif 'CYCLE' in globals() and hasattr(CYCLE, 'status_text'):\n")
hb.append(body_indent + "            await event.reply(CYCLE.status_text())\n")
hb.append(body_indent + "        else:\n")
hb.append(body_indent + "            await event.reply(\"⚠️ Status not available (CYCLE missing)\")\n")
hb.append(body_indent + "    except Exception as e:\n")
hb.append(body_indent + "        await event.reply(f\"❌ status error: {e}\")\n")
hb.append(body_indent + "    return\n")
hb.append(body_indent + "\n")

add("# /excluded")
add("if cmd == \"/excluded\":")
hb.append(body_indent + "    try:\n")
hb.append(body_indent + "        if 'CYCLE' in globals() and hasattr(CYCLE, 'exclusions_text'):\n")
hb.append(body_indent + "            await event.reply(CYCLE.exclusions_text())\n")
hb.append(body_indent + "        elif 'CYCLE' in globals() and hasattr(CYCLE, 'get_excluded_text'):\n")
hb.append(body_indent + "            await event.reply(CYCLE.get_excluded_text())\n")
hb.append(body_indent + "        else:\n")
hb.append(body_indent + "            await event.reply(\"⚠️ Exclusions view not available\")\n")
hb.append(body_indent + "    except Exception as e:\n")
hb.append(body_indent + "        await event.reply(f\"❌ exclusions error: {e}\")\n")
hb.append(body_indent + "    return\n")
hb.append(body_indent + "\n")

add("# /restart (owner-only if helper exists)")
add("if cmd == \"/restart\":")
hb.append(body_indent + "    try:\n")
hb.append(body_indent + "        if '_is_owner' in globals() and callable(globals().get('_is_owner')):\n")
hb.append(body_indent + "            if not _is_owner(sender):\n")
hb.append(body_indent + "                await event.reply(\"⛔ Not allowed\")\n")
hb.append(body_indent + "                return\n")
hb.append(body_indent + "    except Exception:\n")
hb.append(body_indent + "        pass\n")
hb.append(body_indent + "    await event.reply(\"🔁 Restarting bot service...\")\n")
hb.append(body_indent + "    import os\n")
hb.append(body_indent + "    os.system(\"sudo systemctl restart trident >/dev/null 2>&1 &\")\n")
hb.append(body_indent + "    return\n")

new_lines = lines[:dec_i] + lines[dec_i:def_i+1] + hb + lines[end:]
p.write_text("".join(new_lines), encoding="utf-8")

print(f"✅ Handler rewritten with indent-safe body.")
print(f"Decorator line: {dec_i+1}, handler def line: {def_i+1}, replaced body lines {body_start+1}-{end}")
PY

echo "== Compile sanity =="
PYBIN=""
for cand in "$ROOT/venv/bin/python" "$ROOT/.venv/bin/python" "$(command -v python3)"; do
  if [ -x "$cand" ]; then PYBIN="$cand"; break; fi
done
echo "Using: $PYBIN"
"$PYBIN" -m py_compile bot.py

echo "== Restart service =="
sudo systemctl restart trident
echo "DONE ✅ handler fixed + compiled + service restarted"
