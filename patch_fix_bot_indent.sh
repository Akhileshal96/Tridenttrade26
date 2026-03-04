#!/usr/bin/env bash
set -e

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

TS="$(date +%F-%H%M%S)"
BK="$ROOT/backups/$TS-bot-indent-fix"
mkdir -p "$BK"

echo "== Backup bot.py =="
cp -a bot.py "$BK/bot.py"

echo "== Fixing orphan if/elif blocks (restart injection leftovers) =="
python3 - <<'PY'
from pathlib import Path
import re

p = Path("bot.py")
lines = p.read_text().splitlines(True)

out = []
i = 0
changed = False

# We only fix VERY SPECIFIC cases caused by bad /restart patch:
# - lines like: if text.strip() == "/restart":
# - lines like: elif cmd == "/restart":
# that have NO indented body after them (next non-empty line is not more indented)
def indent(s: str) -> int:
    return len(s) - len(s.lstrip(' '))

target_if = re.compile(r'^\s*(if|elif)\s+.*"/restart".*:\s*$')

while i < len(lines):
    line = lines[i]
    out.append(line)

    if target_if.match(line):
        base = indent(line)
        # find next non-empty line
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            out.append(lines[j])
            j += 1

        # if file ends OR next line is not indented more => orphan block
        if j >= len(lines) or indent(lines[j]) <= base:
            out.append(" " * (base + 4) + "pass  # auto-fix orphan /restart block\n")
            changed = True
            i = j
            continue

    i += 1

if changed:
    p.write_text("".join(out))
    print("✅ bot.py orphan /restart blocks fixed (pass inserted).")
else:
    print("ℹ️ No orphan /restart blocks found. No change made.")
PY

echo "== Compile sanity =="
./venv/bin/python -m py_compile bot.py trading_cycle.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ bot.py indentation fixed + service restarted"
echo "Backup at: $BK"
