#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-cycle-status-restart-excluded"
mkdir -p "$BK"

echo "== Backup =="
cp -a "$ROOT/trading_cycle.py" "$BK/trading_cycle.py" || true
cp -a "$ROOT/bot.py"          "$BK/bot.py"          || true
cp -a "$ROOT/config.py"       "$BK/config.py"       || true
echo "Backup saved to: $BK"

echo "== Patch trading_cycle.py (safe helpers + status + exclusions + None-universe guard) =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/trading_cycle.py")
s = p.read_text(encoding="utf-8", errors="replace")

# 1) Ensure STATE has required keys (initiated, live_override, day_key, peak, last_* for status)
def ensure_state_keys(txt: str) -> str:
    m = re.search(r"(?ms)^STATE\s*=\s*\{.*?\n\}\s*$", txt)
    if not m:
        # If STATE block not found, add a safe one near top (after imports)
        insert_at = 0
        mi = re.search(r"(?m)^import\s+config\s+as\s+CFG\s*$", txt)
        if mi:
            insert_at = mi.end()
        state_block = """

# --------------------------
# Runtime State (safe defaults)
# --------------------------
STATE = {
    "paused": True,
    "initiated": False,        # /initiate toggles this
    "live_override": False,    # runtime live enable
    "open_trade": None,        # dict
    "today_pnl": 0.0,
    "day_key": None,
    "peak": None,
    "last_promote_ts": None,
    "last_promote_msg": "Never promoted",
}
"""
        return txt[:insert_at] + state_block + txt[insert_at:]
    else:
        block = m.group(0)
        # Add keys if missing (simple approach: insert missing lines before closing brace)
        needed = {
            "initiated": "    \"initiated\": False,",
            "live_override": "    \"live_override\": False,",
            "day_key": "    \"day_key\": None,",
            "peak": "    \"peak\": None,",
            "last_promote_ts": "    \"last_promote_ts\": None,",
            "last_promote_msg": "    \"last_promote_msg\": \"Never promoted\",",
        }
        # If dict uses single quotes, still okay — we won’t convert style; we just insert if not present.
        for k, line in needed.items():
            if re.search(rf"(?m)^[ \t]*[\"']{k}[\"']\s*:", block) is None:
                block = re.sub(r"(?ms)\n\}\s*$", "\n" + line + "\n}\n", block)
        return txt[:m.start()] + block + txt[m.end():]

s = ensure_state_keys(s)

# 2) Add is_live_enabled() (robust), and get_status_text(), exclusions helpers
helpers = r"""
# ==========================
# Helpers: LIVE mode + status + exclusions
# ==========================
def is_live_enabled() -> bool:
    # Priority:
    # 1) runtime override from /initiate
    # 2) STATE initiated flag (older flow)
    # 3) config flag (static)
    try:
        if bool(STATE.get("live_override")):
            return True
        if bool(STATE.get("initiated")):
            return True
        return bool(getattr(CFG, "IS_LIVE", False))
    except Exception:
        return False


def _fmt(x, nd=2):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def get_status_text() -> str:
    # This is used by /status
    try:
        from datetime import datetime
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        now = "NA"

    mode = "LIVE" if is_live_enabled() else "PAPER"
    paused = bool(STATE.get("paused"))
    tick_s = getattr(CFG, "TICK_SECONDS", 20)
    es = getattr(CFG, "ENTRY_START", "09:20")
    ee = getattr(CFG, "ENTRY_END", "14:30")

    lines = []
    lines.append(f"🕒 Time: {now}")
    lines.append(f"⚙️ Mode: {mode} | Engine: RUNNING | Paused: {paused}")
    lines.append(f"⏱ Tick: {tick_s}s | Entry window: {es}–{ee}")
    lines.append(f"📈 Today PnL (bot): {_fmt(STATE.get('today_pnl', 0.0), 2)} INR")

    trade = STATE.get("open_trade")
    if not trade:
        lines.append("📊 Open trade: None")
    else:
        sym = trade.get("symbol", "NA")
        side = trade.get("side", "NA")
        qty = trade.get("qty", "NA")
        entry = trade.get("price", trade.get("entry", "NA"))
        ltp = trade.get("ltp", "NA")
        entry_ts = trade.get("entry_time", trade.get("ts", "NA"))

        # pnl% if possible
        pnl_pct = "NA"
        try:
            entryf = float(entry)
            ltpf = float(ltp)
            pnl_pct = ((ltpf - entryf) / entryf) * 100.0
            pnl_pct = f"{pnl_pct:.2f}%"
        except Exception:
            pass

        lines.append(f"📊 Open trade: {sym} | side={side} | qty={qty}")
        lines.append(f"🧾 Entry: {entry} | LTP: {ltp} | PnL%: {pnl_pct}")
        lines.append(f"🧾 Entry time: {entry_ts}")

    peak = STATE.get("peak", None)
    peak_txt = "NA" if peak in (None, "NA") else f"{_fmt(peak,2)}%"
    act = getattr(CFG, "PROFIT_LOCK_ACTIVATE_PCT", 1.5)
    trail = getattr(CFG, "PROFIT_LOCK_TRAIL_PCT", 2.0)
    lines.append(f"🔒 Profit lock: peak={peak_txt} | activates at {act:.2f}% | trail {trail:.2f}%")

    # last promote info (if your universe auto-promote exists)
    lp = STATE.get("last_promote_msg", None)
    if lp:
        lines.append(f"🧠 Last promote: {lp}")

    return "\n".join(lines)


# --------------------------
# Exclusions (used by /excluded)
# --------------------------
def _exclusions_file():
    import os
    data_dir = getattr(CFG, "DATA_DIR", os.path.join(os.getcwd(), "data"))
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        pass
    return getattr(CFG, "EXCLUSIONS_FILE", os.path.join(data_dir, "exclusions.txt"))


def load_exclusions() -> list:
    fp = _exclusions_file()
    try:
        import os
        if not os.path.exists(fp):
            return []
        rows = []
        for line in open(fp, "r", encoding="utf-8", errors="ignore"):
            t = line.strip().upper()
            if not t or t.startswith("#"):
                continue
            rows.append(t)
        return sorted(set(rows))
    except Exception:
        return []


def save_exclusions(items: list) -> None:
    fp = _exclusions_file()
    try:
        uniq = sorted(set([str(x).strip().upper() for x in items if str(x).strip()]))
        with open(fp, "w", encoding="utf-8") as f:
            for x in uniq:
                f.write(x + "\n")
    except Exception:
        pass


def exclusions_text() -> str:
    items = load_exclusions()
    if not items:
        return "📌 Exclusions: (empty)"
    return "📌 Exclusions:\n" + "\n".join([f"• {x}" for x in items])


def add_exclusion(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /excluded add SBIN"
    items = load_exclusions()
    if sym in items:
        return f"✅ Already excluded: {sym}"
    items.append(sym)
    save_exclusions(items)
    return f"✅ Excluded added: {sym}"


def remove_exclusion(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return "Usage: /excluded rm SBIN"
    items = load_exclusions()
    if sym not in items:
        return f"⚠️ Not in exclusions: {sym}"
    items = [x for x in items if x != sym]
    save_exclusions(items)
    return f"✅ Excluded removed: {sym}"
"""

# Insert helpers only once
if "def get_status_text()" not in s:
    # Place helpers after imports (after "from ... import ..." area)
    insert_pos = 0
    mi = re.search(r"(?ms)\A(.*?\n)(?=\n\S)", s)  # first block
    if mi:
        insert_pos = mi.end(1)
    s = s[:insert_pos] + "\n" + helpers + "\n" + s[insert_pos:]

# 3) Guard universe NoneType -> []
# Try to find generate_signal(universe) usage and ensure universe defaults
# We'll add a tiny wrapper if not present.
if "def _safe_universe(" not in s:
    safe_u = r"""
def _safe_universe(u):
    # Avoid NoneType iterable crash
    try:
        return u if u is not None else []
    except Exception:
        return []
"""
    # put near helpers
    s = s.replace("def get_status_text()", safe_u + "\n\ndef get_status_text()", 1)

# Replace any direct "universe" iteration risky patterns minimally
# (If code has: for sym in universe: -> ensure universe = _safe_universe(universe) somewhere)
# We patch inside tick(): after it starts, set universe safe if variable exists later.
mt = re.search(r"(?ms)^def\s+tick\s*\(\s*\)\s*:\s*\n", s)
if mt and "_safe_universe(" not in s[mt.start():mt.start()+2000]:
    # We'll insert a harmless line early in tick() that doesn't break anything:
    # universe = _safe_universe(universe) only if universe exists -> so we use try/except.
    inject = "    try:\n        universe = _safe_universe(locals().get('universe'))\n    except Exception:\n        pass\n\n"
    # insert after first log line in tick if present
    s2 = re.sub(r"(?ms)^(def\s+tick\s*\(\s*\)\s*:\s*\n(?:[ \t]+.*\n){0,3})",
                r"\1" + inject, s, count=1)
    s = s2

p.write_text(s, encoding="utf-8")
print("trading_cycle.py patched OK")
PY

echo "== Patch bot.py (/restart + /excluded handlers, safe) =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/bot.py")
s = p.read_text(encoding="utf-8", errors="replace")

# Ensure we import os at top (for restart)
if re.search(r"(?m)^\s*import\s+os\s*$", s) is None:
    # add after existing imports
    s = re.sub(r"(?m)^(import\s+asyncio\s*\n)", r"\1import os\n", s, count=1)

# Ensure trading_cycle imported as CYCLE already (your file shows it exists)
if "import trading_cycle as CYCLE" not in s:
    s = re.sub(r"(?m)^(import\s+asyncio.*\n)", r"\1import trading_cycle as CYCLE\n", s, count=1)

# We patch inside the message handler where cmd is compared.
# We will inject two elif blocks:
# - /excluded (view/add/rm)
# - /restart (attempt sudo -n systemctl restart trident)
excluded_block = r"""
        elif cmd == "/excluded":
            # Usage:
            # /excluded            -> list
            # /excluded add SBIN   -> add
            # /excluded rm SBIN    -> remove
            parts = text.strip().split()
            if len(parts) == 1:
                await event.reply(CYCLE.exclusions_text())
            elif len(parts) >= 3 and parts[1].lower() in ("add", "a", "+"):
                await event.reply(CYCLE.add_exclusion(parts[2]))
            elif len(parts) >= 3 and parts[1].lower() in ("rm", "remove", "del", "-"):
                await event.reply(CYCLE.remove_exclusion(parts[2]))
            else:
                await event.reply("Usage:\n/excluded\n/excluded add SBIN\n/excluded rm SBIN")
            return
"""

restart_block = r"""
        elif cmd == "/restart":
            await event.reply("🔄 Restart requested…")
            # Try non-interactive sudo restart (works only if sudoers allows it)
            try:
                rc = os.system("sudo -n systemctl restart trident >/dev/null 2>&1")
                if rc == 0:
                    return
            except Exception:
                pass
            await event.reply("⚠️ Could not restart automatically.\nRun this on server:\nsudo systemctl restart trident")
            return
"""

# Find where commands are handled:
# We’ll locate a common pattern: `cmd = text.strip().split()[0].lower()` then `if cmd == ...`
m = re.search(r"(?ms)^\s*cmd\s*=\s*.*?\n\s*if\s+cmd\s*==\s*['\"]/start['\"]\s*:\s*\n", s)
if not m:
    # fallback: inject before unknown command reply
    # Try locate the `await event.reply("Unknown command...")`
    um = re.search(r'(?ms)^\s*await\s+event\.reply\(\s*["\']Unknown command.*?\)\s*$', s)
    if not um:
        raise SystemExit("Could not find command router in bot.py")
    # insert before unknown reply with same indent
    indent = re.match(r"^(\s*)", s[um.start():], re.M).group(1)
    s = s[:um.start()] + excluded_block.replace("        ", indent) + restart_block.replace("        ", indent) + "\n" + s[um.start():]
else:
    # Insert after /status block if present, else after first if
    # We’ll insert right after the /status handler if it exists.
    status_pat = r"(?ms)^\s*elif\s+cmd\s*==\s*['\"]/status['\"]\s*:\s*\n.*?\n\s*return\s*\n"
    ms = re.search(status_pat, s)
    if ms:
        ins_at = ms.end()
        s = s[:ins_at] + "\n" + excluded_block + "\n" + restart_block + s[ins_at:]
    else:
        # Insert after first if cmd == "/start"
        first_if = re.search(r"(?ms)^\s*if\s+cmd\s*==\s*['\"]/start['\"].*?\n\s*return\s*\n", s)
        if first_if:
            ins_at = first_if.end()
            s = s[:ins_at] + "\n" + excluded_block + "\n" + restart_block + s[ins_at:]
        else:
            raise SystemExit("Could not locate insertion point in bot.py")

# Ensure /status uses get_status_text if available
# Replace any event.reply that only prints open trade
s = re.sub(
    r"(?ms)(elif\s+cmd\s*==\s*['\"]/status['\"]\s*:\s*\n)(.*?await\s+event\.reply\()(.*?)(\)\s*\n\s*return)",
    r"\1\2CYCLE.get_status_text()\4",
    s,
    count=1
)

p.write_text(s, encoding="utf-8")
print("bot.py patched OK")
PY

echo "== Compile sanity =="
cd "$ROOT"
./.venv/bin/python -m py_compile trading_cycle.py
./.venv/bin/python -m py_compile bot.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ Patch applied."
echo "If anything goes wrong, restore backups from: $BK"
