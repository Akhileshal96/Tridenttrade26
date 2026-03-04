#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
SERVICE_PATH="/etc/systemd/system/trident.service"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}_v5_allinone"
mkdir -p "$BK"

echo "==[1/8] Backing up files to $BK =="
for f in bot.py trading_cycle.py strategy_engine.py broker_zerodha.py config.py log_store.py; do
  [ -f "$ROOT/$f" ] && cp -a "$ROOT/$f" "$BK/$f" || true
done
[ -f "$SERVICE_PATH" ] && sudo cp -a "$SERVICE_PATH" "$BK/trident.service" || true

echo "==[2/8] Set server timezone to Asia/Kolkata (fix wrong log timestamps) =="
sudo timedatectl set-timezone Asia/Kolkata || true

echo "==[3/8] Patch systemd service for live logs + TZ + unbuffered Python =="
if [ -f "$SERVICE_PATH" ]; then
  sudo sed -i 's|^\s*ExecStart=.*python \([^ ]*bot\.py\)\s*$|ExecStart=/home/ubuntu/trident-bot/venv/bin/python -u \1|g' "$SERVICE_PATH" || true
  # If ExecStart uses full path, enforce it:
  if ! grep -q 'ExecStart=.*/venv/bin/python -u ' "$SERVICE_PATH"; then
    sudo sed -i 's|^\s*ExecStart=.*python .*bot\.py\s*$|ExecStart=/home/ubuntu/trident-bot/venv/bin/python -u /home/ubuntu/trident-bot/bot.py|g' "$SERVICE_PATH" || true
  fi

  # Ensure Environment TZ + PYTHONUNBUFFERED
  sudo grep -q '^Environment=TZ=Asia/Kolkata' "$SERVICE_PATH" || sudo sed -i '/^\[Service\]/a Environment=TZ=Asia\/Kolkata' "$SERVICE_PATH"
  sudo grep -q '^Environment=PYTHONUNBUFFERED=1' "$SERVICE_PATH" || sudo sed -i '/^\[Service\]/a Environment=PYTHONUNBUFFERED=1' "$SERVICE_PATH"

  # Ensure journald captures stdout/stderr
  sudo grep -q '^StandardOutput=journal' "$SERVICE_PATH" || sudo sed -i '/^\[Service\]/a StandardOutput=journal' "$SERVICE_PATH"
  sudo grep -q '^StandardError=journal' "$SERVICE_PATH" || sudo sed -i '/^\[Service\]/a StandardError=journal' "$SERVICE_PATH"
else
  echo "WARN: $SERVICE_PATH not found, skipping systemd edits"
fi

echo "==[4/8] Patch config.py to support token auto-restart flag (Option A) =="
python3 - <<'PY'
import os, re
p="/home/ubuntu/trident-bot/config.py"
if not os.path.exists(p):
    raise SystemExit("config.py not found")

s=open(p,"r",encoding="utf-8").read()

def has(name): 
    return re.search(rf'^\s*{re.escape(name)}\s*=', s, flags=re.M) is not None

# Try to detect helper name used in your config: _get_str/_get_bool.
# If missing, we still append constants safely with os.getenv fallback.
if "_get_str" in s:
    add1='RESTART_FLAG_PATH = _get_str("RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")'
else:
    add1='RESTART_FLAG_PATH = os.getenv("RESTART_FLAG_PATH","/home/ubuntu/trident-bot/RESTART_REQUIRED")'
if "_get_bool" in s:
    add2='ENABLE_TOKEN_AUTORESTART = _get_bool("ENABLE_TOKEN_AUTORESTART", True)'
else:
    add2='ENABLE_TOKEN_AUTORESTART = (os.getenv("ENABLE_TOKEN_AUTORESTART","true").lower().strip()=="true")'

# Ensure os imported
if "import os" not in s.splitlines()[0:10]:
    s = "import os\n" + s

if not has("RESTART_FLAG_PATH"):
    s += ("\n" if not s.endswith("\n") else "") + add1 + "\n"
if not has("ENABLE_TOKEN_AUTORESTART"):
    s += add2 + "\n"

open(p,"w",encoding="utf-8").write(s)
print("config.py OK")
PY

echo "==[5/8] Patch trading_cycle.py: exit cleanly when RESTART_REQUIRED exists =="
python3 - <<'PY'
import os, re
p="/home/ubuntu/trident-bot/trading_cycle.py"
if not os.path.exists(p):
    raise SystemExit("trading_cycle.py not found")

s=open(p,"r",encoding="utf-8").read()

if "def _check_restart_flag" not in s:
    if "import os" not in s:
        s = "import os\n" + s
    if "import config as CFG" not in s:
        s = "import config as CFG\n" + s

    s += """
def _check_restart_flag():
    try:
        flag_path = getattr(CFG, "RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")
        return os.path.exists(flag_path), flag_path
    except Exception:
        return False, "/home/ubuntu/trident-bot/RESTART_REQUIRED"
"""

# Inject into main loop: before each tick, check flag.
# Works if file contains 'while True:' loop.
if "Restart flag detected" not in s:
    m = re.search(r"\nwhile\s+True\s*:\s*\n", s)
    if m:
        ins = """\n    # Token auto-restart flag (Option A)
    should_restart, flag_path = _check_restart_flag()
    if should_restart:
        try:
            from log_store import append_log
            append_log("INFO", "RESTART", f"Restart flag detected at {flag_path}. Exiting for systemd restart.")
        except Exception:
            pass
        try:
            os.remove(flag_path)
        except Exception:
            pass
        raise SystemExit(0)\n
"""
        s = s[:m.end()] + ins + s[m.end():]

# Add execution breadcrumbs (so you can see what's executing)
if "[CYCLE]" not in s and "append_log(\"INFO\", \"CYCLE\"" not in s:
    # Try to add near start of tick() if present
    s = re.sub(
        r"(def\s+tick\s*\(\s*\)\s*:\s*\n)",
        r"\1    from log_store import append_log\n    append_log(\"INFO\",\"CYCLE\",\"tick() entered\")\n",
        s,
        count=1
    )

open(p,"w",encoding="utf-8").write(s)
print("trading_cycle.py OK")
PY

echo "==[6/8] Patch bot.py: after /token, create restart flag so systemd restarts automatically =="
python3 - <<'PY'
import os, re
p="/home/ubuntu/trident-bot/bot.py"
if not os.path.exists(p):
    raise SystemExit("bot.py not found")

s=open(p,"r",encoding="utf-8").read()
if "import os" not in s:
    s = "import os\n" + s
if "import config as CFG" not in s:
    # add after initial imports
    s = re.sub(r"^(import[^\n]*\n)+", lambda m: m.group(0)+"import config as CFG\n", s, count=1, flags=re.M)

# Add command logging (visibility)
if "append_log(\"INFO\",\"TG\"" not in s:
    s = s.replace(
        "cmd = event.raw_text",
        "cmd = event.raw_text\n        append_log(\"INFO\",\"TG\", f\"cmd={cmd} from={event.sender_id}\")"
    )

# Add restart flag creation after token success message (best-effort string match)
if "RESTART_REQUIRED" not in s and "RESTART_FLAG_PATH" not in s:
    # We insert a helper function and rely on you calling it after successful token save
    s += """
def _request_restart_flag():
    try:
        if getattr(CFG, "ENABLE_TOKEN_AUTORESTART", True):
            flag_path = getattr(CFG, "RESTART_FLAG_PATH", "/home/ubuntu/trident-bot/RESTART_REQUIRED")
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("restart\\n")
            append_log("INFO","RESTART","Restart flag created after /token. systemd will restart the bot.")
    except Exception as e:
        append_log("ERROR","RESTART", f"Failed to create restart flag: {e}")
"""

# If your /token handler sends a success reply, we try to hook right before that.
# Very common line:
# await event.reply("✅ Access token updated ...")
if "_request_restart_flag()" not in s:
    s = re.sub(
        r"(await\s+event\.reply\([\"']✅\s*Access\s*token.*?\)\s*)",
        r"_request_restart_flag()\n        \1",
        s,
        count=1,
        flags=re.S
    )

open(p,"w",encoding="utf-8").write(s)
print("bot.py OK")
PY

echo "==[7/8] Patch strategy_engine.py: ensure SCAN logs + errors show per symbol =="
python3 - <<'PY'
import os, re
p="/home/ubuntu/trident-bot/strategy_engine.py"
if not os.path.exists(p):
    print("strategy_engine.py not found, skipping")
    raise SystemExit(0)

s=open(p,"r",encoding="utf-8").read()

# Ensure append_log imported
if "append_log" not in s:
    s = "from log_store import append_log\n" + s

# Add SCAN log inside for sym loop (best effort)
if "Scanning {sym}" not in s:
    s = re.sub(
        r"(for\s+sym\s+in\s+universe\s*:\s*\n)",
        r"\1        append_log(\"INFO\",\"SCAN\", f\"Scanning {sym}\")\n",
        s,
        count=1
    )

# Add exception logging if it is swallowing exceptions
if "except" in s and "continue" in s and "append_log" not in s.split("except",1)[1]:
    # Not perfect; we only add if completely silent.
    s = s.replace("except: continue", "except Exception as e:\n            append_log(\"WARN\",\"SCAN\", f\"{sym} error: {e}\")\n            continue")

open(p,"w",encoding="utf-8").write(s)
print("strategy_engine.py OK")
PY

echo "==[8/8] Reload systemd + restart service =="
sudo systemctl daemon-reload || true
sudo systemctl restart trident || true

echo "DONE ✅ v5 all-in-one upgrade applied."
echo ""
echo "LIVE LOG COMMANDS:"
echo "  sudo journalctl -u trident -f -o cat"
echo "  sudo journalctl -u trident -f -o cat | egrep \"TG|CYCLE|SCAN|SIG|ORDER|EXIT|TOKEN|RESTART|ERROR|WARN\""
