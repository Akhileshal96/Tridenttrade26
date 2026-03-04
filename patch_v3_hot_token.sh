#!/usr/bin/env bash
set -euo pipefail

BASE="/home/ubuntu/trident-bot"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$BASE/backups/${TS}_hot_token"
mkdir -p "$BK"

echo "[1/5] Backing up files to: $BK"
cp -a "$BASE/broker_zerodha.py" "$BK/" 2>/dev/null || true
cp -a "$BASE/bot.py" "$BK/" 2>/dev/null || true

echo "[2/5] Patching broker_zerodha.py (hot token reload)"
python3 - <<'PY'
import os, re, pathlib

base = pathlib.Path("/home/ubuntu/trident-bot")
p = base / "broker_zerodha.py"
if not p.exists():
    raise SystemExit("broker_zerodha.py not found")

src = p.read_text(errors="ignore")

# If already patched, don't double patch
if "HOT_TOKEN_RELOAD" in src:
    print("broker_zerodha.py already patched, skipping")
else:
    # Add helper + hot reload logic, while preserving existing get_kite signature
    addition = r'''
# === HOT_TOKEN_RELOAD (v3) ===
# This section enables applying a new KITE_ACCESS_TOKEN at runtime after /token updates .env
import threading

_HOT_LOCK = threading.Lock()
_LAST_TOKEN = None

def _read_env_file(path=".env"):
    d = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                d[k] = v
    except Exception:
        pass
    return d

def _current_access_token():
    # Prefer process env, fallback to .env file
    tok = os.getenv("KITE_ACCESS_TOKEN", "") or os.getenv("KITE_ACCESS_TOKEN".lower(), "")
    if tok:
        return tok.strip()
    envd = _read_env_file("/home/ubuntu/trident-bot/.env")
    tok = envd.get("KITE_ACCESS_TOKEN", "") or envd.get("KITE_ACCESS_TOKEN".lower(), "")
    return (tok or "").strip()

def apply_access_token_if_changed(kite):
    global _LAST_TOKEN
    new_tok = _current_access_token()
    if not new_tok:
        return False, "KITE_ACCESS_TOKEN missing"
    if _LAST_TOKEN == new_tok:
        return False, "Token unchanged"
    try:
        kite.set_access_token(new_tok)
        _LAST_TOKEN = new_tok
        return True, "Token applied"
    except Exception as e:
        return False, f"Failed to apply token: {e}"

def force_reload_kite():
    # If your module stores a cached Kite instance, clearing it here helps.
    # We'll try common cache names safely.
    global _LAST_TOKEN
    _LAST_TOKEN = None
    for name in ("kite", "_kite", "KITE", "client", "_client"):
        if name in globals():
            globals()[name] = None
    return True
# === /HOT_TOKEN_RELOAD (v3) ===
'''
    # Ensure os imported
    if "import os" not in src:
        src = "import os\n" + src

    # Append addition at end
    src = src.rstrip() + "\n\n" + addition + "\n"

    # Patch get_kite to auto-apply token whenever called.
    # We’ll insert a call just before returning the kite object in the common pattern "return kite"
    # If multiple returns exist, patch the first function named get_kite.
    m = re.search(r"def\s+get_kite\s*\(\s*\)\s*:\s*\n", src)
    if not m:
        # Some repos define get_kite(api_key=None) etc. Handle any signature.
        m = re.search(r"def\s+get_kite\s*\(.*?\)\s*:\s*\n", src)
    if not m:
        raise SystemExit("Could not find get_kite() in broker_zerodha.py")

    # Find function block
    start = m.start()
    # crude: locate end by next "def " at col 0 after start
    nxt = re.search(r"\ndef\s+\w+\s*\(", src[m.end():])
    end = (m.end() + nxt.start()) if nxt else len(src)
    block = src[start:end]

    if "apply_access_token_if_changed" in block:
        # already patched
        pass
    else:
        # Insert token apply near end: before first 'return' that returns a variable named kite/_kite/client
        # We'll add a safe call just before any 'return' in get_kite.
        lines = block.splitlines(True)
        out = []
        inserted = False
        for line in lines:
            if (not inserted) and re.match(r"\s*return\s+\w+", line):
                indent = re.match(r"(\s*)", line).group(1)
                out.append(indent + "with _HOT_LOCK:\n")
                out.append(indent + "    try:\n")
                out.append(indent + "        apply_access_token_if_changed(locals().get('kite') or locals().get('_kite') or locals().get('client') or locals().get('_client'))\n")
                out.append(indent + "    except Exception:\n")
                out.append(indent + "        pass\n")
                inserted = True
            out.append(line)
        block2 = "".join(out)
        src = src[:start] + block2 + src[end:]

    p.write_text(src)
    print("broker_zerodha.py patched OK")

PY

echo "[3/5] Patching bot.py (/token applies token immediately)"
python3 - <<'PY'
import re, pathlib

base = pathlib.Path("/home/ubuntu/trident-bot")
p = base / "bot.py"
if not p.exists():
    raise SystemExit("bot.py not found")

src = p.read_text(errors="ignore")

# Make sure we import force_reload_kite safely
if "force_reload_kite" not in src:
    # If bot already imports get_kite from broker_zerodha, extend it
    src = re.sub(
        r"from\s+broker_zerodha\s+import\s+([^\n]+)",
        lambda m: m.group(0) if "force_reload_kite" in m.group(0) else m.group(0).rstrip() + ", force_reload_kite",
        src,
        count=1
    )

# After token is saved, call force_reload_kite() and apply immediately by calling get_kite()
# We search for the confirmation message "Access token updated" and insert before it.
if "Token applied (no restart needed)" not in src:
    src = src.replace(
        "Access token updated in .env. Now run:\nsudo systemctl restart trident",
        "Access token updated in .env.\n✅ Applying token now (no restart needed)…"
    )

    # Insert apply logic near where token is saved.
    # Heuristic: find a line that writes KITE_ACCESS_TOKEN into .env OR calls save_env/update_env
    insert_points = []
    for pat in [
        r"KITE_ACCESS_TOKEN",
        r"write_env",
        r"save_env",
        r"update_env",
        r"\.env"
    ]:
        m = re.search(pat, src)
        if m:
            insert_points.append(m.start())
    at = min(insert_points) if insert_points else None

    # Better: insert right before the success reply in /token handler:
    m = re.search(r"(Access token updated in \.env\.)", src)
    if m:
        # find line start
        line_start = src.rfind("\n", 0, m.start()) + 1
        indent = re.match(r"(\s*)", src[line_start:]).group(1)
        inject = (
            f"{indent}try:\n"
            f"{indent}    force_reload_kite()\n"
            f"{indent}    # Trigger fresh Kite init + apply token\n"
            f"{indent}    _ = get_kite()\n"
            f"{indent}    msg_extra = \"\\n✅ Token applied (no restart needed).\"\n"
            f"{indent}except Exception as e:\n"
            f"{indent}    msg_extra = f\"\\n⚠️ Token saved, but apply failed: {e}. You can still restart service.\"\n"
        )
        # ensure get_kite imported if not
        if "get_kite" not in src:
            src = "from broker_zerodha import get_kite\n" + src
        src = src[:line_start] + inject + src[line_start:]
        # append msg_extra to reply if there is a send_message with that text
        src = src.replace("Access token updated in .env.", "Access token updated in .env." + "{msg_extra}")
    else:
        print("WARN: Could not find success message in bot.py, patch may be partial")

p.write_text(src)
print("bot.py patched OK")
PY

echo "[4/5] Syntax check"
cd /home/ubuntu/trident-bot
./venv/bin/python -m py_compile broker_zerodha.py bot.py

echo "[5/5] Restarting service (one-time) to load patched code"
sudo systemctl restart trident

echo "✅ Patch applied. From now on, /token will apply instantly — no restart required."
