#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/$TS-verbose+excluded"
mkdir -p "$BK"

echo "== Backup =="
for f in log_store.py trading_cycle.py bot.py strategy_engine.py excluded_store.py; do
  if [ -f "$ROOT/$f" ]; then
    cp -a "$ROOT/$f" "$BK/$f"
    echo "  backed up $f"
  fi
done

echo "== (1/2) Verbose logging: rewrite log_store.py (file + console + LOG_LEVEL + IST timestamps) =="
cat > "$ROOT/log_store.py" <<'PY'
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trident.log")

def _level():
    lvl = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, lvl, logging.INFO)

class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

logger = logging.getLogger("Trident")
logger.setLevel(_level())

if not logger.handlers:
    fmt = ISTFormatter("%(asctime)s | %(levelname)s | %(message)s")

    # File handler
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(_level())
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler -> appears in `journalctl -u trident -f`
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(_level())
    ch.setFormatter(fmt)
    logger.addHandler(ch)

def append_log(level, tag, message):
    msg = f"[{tag}] {message}"
    level = (level or "INFO").upper()
    if level == "DEBUG":
        logger.debug(msg)
    elif level == "INFO":
        logger.info(msg)
    elif level in ("WARN", "WARNING"):
        logger.warning(msg)
    elif level == "ERROR":
        logger.error(msg)
    else:
        logger.info(msg)

def tail_text(n: int):
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        return "".join(f.readlines()[-n:])

def export_all():
    return LOG_FILE

def tail_today():
    """Return today's log lines (IST) as text."""
    if not os.path.exists(LOG_FILE):
        return "(no logs yet)"
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = []
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            if ln.startswith(today):
                out.append(ln)
    return "".join(out) if out else "(no logs for today yet)"
PY

echo "== Inject verbose scan/signal logs into trading_cycle.py (non-destructive) =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/trading_cycle.py")
if not p.exists():
    print("trading_cycle.py not found -> skipping verbose injection")
    raise SystemExit(0)

s = p.read_text(encoding="utf-8")

# ensure traceback import
if "import traceback" not in s:
    # insert after "import time" if possible, else at top
    if re.search(r"^import\s+time\s*$", s, flags=re.M):
        s = re.sub(r"(^import\s+time\s*$)", r"\1\nimport traceback", s, flags=re.M)
    else:
        s = "import traceback\n" + s

# add tick start debug if not present
if re.search(r"^\s*def\s+tick\s*\(", s, flags=re.M) and "tick start" not in s:
    s = re.sub(r"(^\s*def\s+tick\s*\(.*?\)\s*:\s*\n)", r"\1    append_log('DEBUG','TICK','tick start')\n", s, flags=re.M)

# add universe size logs right before generate_signal(universe) if pattern exists
if "generate_signal(" in s and "Universe size=" not in s:
    s = re.sub(
        r"(\s*)sig\s*=\s*generate_signal\(\s*universe\s*\)",
        r"\1append_log('INFO','SCAN',f'Universe size={len(universe)}')\n\1append_log('DEBUG','SCAN',f'Universe sample={universe[:10]}')\n\1sig = generate_signal(universe)",
        s
    )

# log when no signal
if "No signal (generate_signal returned None)" not in s:
    s = re.sub(
        r"(\s*)if\s+not\s+sig\s*:\s*\n(\s*)return",
        r"\1if not sig:\n\2append_log('INFO','SIG','No signal (generate_signal returned None)')\n\2return",
        s
    )

# log when signal found (if it assigns open_trade)
if "Signal found:" not in s:
    s = re.sub(
        r"(\s*)STATE\[['\"]open_trade['\"]\]\s*=\s*sig",
        r"\1append_log('INFO','SIG',f\"Signal found: {sig}\")\n\1STATE['open_trade'] = sig",
        s
    )

# add traceback in tick exception blocks if "tick exception" exists
if "traceback.format_exc()" not in s and "tick exception" in s:
    s = re.sub(
        r"(append_log\(\s*['\"]ERROR['\"],\s*['\"]TICK['\"],\s*f?['\"].*tick exception.*['\"]\s*\))",
        r"\1\n        append_log('ERROR','TICK', traceback.format_exc())",
        s
    )

p.write_text(s, encoding="utf-8")
print("OK: trading_cycle.py verbose logging injected (where patterns matched)")
PY

echo "== (2/2) Excluded symbols store + commands =="
mkdir -p "$ROOT/data"

cat > "$ROOT/excluded_store.py" <<'PY'
import os
from typing import Set

EXCL_PATH = os.path.join(os.getcwd(), "data", "excluded.txt")
os.makedirs(os.path.dirname(EXCL_PATH), exist_ok=True)

def load_excluded() -> Set[str]:
    if not os.path.exists(EXCL_PATH):
        return set()
    with open(EXCL_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return {ln.strip().upper() for ln in f.read().splitlines() if ln.strip() and not ln.strip().startswith("#")}

def save_excluded(items: Set[str]) -> None:
    with open(EXCL_PATH, "w", encoding="utf-8") as f:
        for s in sorted(items):
            f.write(s + "\n")

def add_symbol(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    if not sym:
        return False
    s = load_excluded()
    if sym in s:
        return False
    s.add(sym)
    save_excluded(s)
    return True

def remove_symbol(sym: str) -> bool:
    sym = (sym or "").strip().upper()
    if not sym:
        return False
    s = load_excluded()
    if sym not in s:
        return False
    s.remove(sym)
    save_excluded(s)
    return True
PY

echo "== Patch bot.py: add /exclude /include /excluded handlers =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
if not p.exists():
    raise SystemExit("bot.py not found")

s = p.read_text(encoding="utf-8")

# Ensure import excluded_store
if "import excluded_store" not in s:
    # place after existing imports
    m = re.search(r"^(import .*?\n)+", s, flags=re.M)
    if m:
        s = s[:m.end()] + "import excluded_store\n" + s[m.end():]
    else:
        s = "import excluded_store\n" + s

# If already contains /excluded, skip
if "/excluded" in s:
    p.write_text(s, encoding="utf-8")
    print("bot.py already has /excluded handlers; skipping insert")
    raise SystemExit(0)

inject = r'''
        elif cmd.startswith("/exclude"):
            parts = cmd.split()
            if len(parts) < 2:
                await event.reply("Usage: /exclude SYMBOL\nExample: /exclude SBIN")
            else:
                sym = parts[1].strip().upper()
                ok = excluded_store.add_symbol(sym)
                await event.reply(f"🚫 Excluded: {sym}" if ok else f"ℹ️ Already excluded: {sym}")

        elif cmd.startswith("/include"):
            parts = cmd.split()
            if len(parts) < 2:
                await event.reply("Usage: /include SYMBOL\nExample: /include SBIN")
            else:
                sym = parts[1].strip().upper()
                ok = excluded_store.remove_symbol(sym)
                await event.reply(f"✅ Included back: {sym}" if ok else f"ℹ️ Not in excluded list: {sym}")

        elif cmd.strip() == "/excluded":
            items = sorted(excluded_store.load_excluded())
            if not items:
                await event.reply("✅ Excluded list is empty")
            else:
                txt = "🚫 Excluded symbols (" + str(len(items)) + "):\n" + "\n".join(items[:200])
                if len(items) > 200:
                    txt += f"\n...and {len(items)-200} more"
                await event.reply(txt)
'''

# Insert after /logs handler if present, else after /status, else before end.
if re.search(r'elif\s+cmd\s*==\s*"/logs"', s):
    s = re.sub(r'(elif\s+cmd\s*==\s*"/logs".*?\n)', r'\1' + inject + "\n", s, count=1, flags=re.S)
elif re.search(r'elif\s+cmd\s*==\s*"/status"', s):
    s = re.sub(r'(elif\s+cmd\s*==\s*"/status".*?\n)', r'\1' + inject + "\n", s, count=1, flags=re.S)
else:
    s = re.sub(r'(\n\s+await\s+asyncio\.gather\()', inject + r'\1', s, count=1)

p.write_text(s, encoding="utf-8")
print("OK: bot.py patched with /exclude /include /excluded")
PY

echo "== Patch strategy_engine.py: skip excluded symbols (only if file exists) =="
if [ -f "$ROOT/strategy_engine.py" ]; then
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/strategy_engine.py")
s = p.read_text(encoding="utf-8")

if "import excluded_store" not in s:
    s = "import excluded_store\n" + s

# only inject once
if "insider safety: skip excluded symbols" not in s:
    pat = r"(for\s+sym\s+in\s+universe\s*:\s*\n)"
    if re.search(pat, s):
        s = re.sub(
            pat,
            r"\1        # insider safety: skip excluded symbols\n        if str(sym).strip().upper() in excluded_store.load_excluded():\n            continue\n",
            s,
            count=1
        )

p.write_text(s, encoding="utf-8")
print("OK: strategy_engine.py exclusion skip injected (if loop pattern matched)")
PY
fi

echo "== Compile check (best effort) =="
/home/ubuntu/trident-bot/venv/bin/python -m py_compile /home/ubuntu/trident-bot/log_store.py
[ -f /home/ubuntu/trident-bot/excluded_store.py ] && /home/ubuntu/trident-bot/venv/bin/python -m py_compile /home/ubuntu/trident-bot/excluded_store.py
[ -f /home/ubuntu/trident-bot/trading_cycle.py ] && /home/ubuntu/trident-bot/venv/bin/python -m py_compile /home/ubuntu/trident-bot/trading_cycle.py || true
[ -f /home/ubuntu/trident-bot/bot.py ] && /home/ubuntu/trident-bot/venv/bin/python -m py_compile /home/ubuntu/trident-bot/bot.py || true
[ -f /home/ubuntu/trident-bot/strategy_engine.py ] && /home/ubuntu/trident-bot/venv/bin/python -m py_compile /home/ubuntu/trident-bot/strategy_engine.py || true

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ Combined patch applied."
echo
echo "Turn on max logs (recommended): add LOG_LEVEL=DEBUG to .env then restart"
echo "  sudo journalctl -u trident -f -o cat"
