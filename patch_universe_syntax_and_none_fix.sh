#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
FILE="$ROOT/trading_cycle.py"
TS="$(date +%Y%m%d_%H%M%S)"
BK="$ROOT/backups/${TS}-universe-syntax-none-fix"

mkdir -p "$BK"

if [ ! -f "$FILE" ]; then
  echo "ERROR: $FILE not found"
  exit 1
fi

cp -a "$FILE" "$BK/trading_cycle.py.bak"
echo "Backup saved to: $BK/trading_cycle.py.bak"

python3 - <<'PY'
from pathlib import Path
import re

p = Path("/home/ubuntu/trident-bot/trading_cycle.py")
s = p.read_text(encoding="utf-8").splitlines(True)

def indent_of(line: str) -> str:
    return re.match(r'^(\s*)', line).group(1)

# --- Patch A: Fix "try:" without except/finally around the injected universe block ---
# We detect: a "try:" line, then later an "if not universe:" line, but no except/finally
# before indentation leaves the try block -> insert an "except Exception as e:" block.

# Build line index for quick scanning
try_idxs = [i for i,l in enumerate(s) if re.match(r'^\s*try:\s*$', l)]
changed = False

for ti in try_idxs:
    base_indent = indent_of(s[ti])
    # scan forward until indentation <= base_indent (meaning try block ended)
    j = ti + 1
    seen_if_not_universe = False
    seen_except_or_finally = False

    while j < len(s):
        line = s[j]
        # End of try block: indentation back to base or less (and not blank/comment)
        if line.strip() and (len(indent_of(line)) <= len(base_indent)) and not re.match(r'^\s*(except|finally)\b', line):
            break

        if re.match(r'^\s*(except|finally)\b', line):
            seen_except_or_finally = True
            break

        if re.match(r'^\s*if\s+not\s+universe\s*:\s*$', line):
            seen_if_not_universe = True

        j += 1

    # If this try contains "if not universe:" but has no except/finally => insert except
    if seen_if_not_universe and not seen_except_or_finally:
        # Insert except right before j (end of try block)
        exc_indent = base_indent
        body_indent = exc_indent + "    "
        block = [
            f"{exc_indent}except Exception as e:\n",
            f"{body_indent}try:\n",
            f"{body_indent}    append_log('ERROR','SCAN',f'universe block failed: {{}!s}'.format(e))\n",
            f"{body_indent}except Exception:\n",
            f"{body_indent}    pass\n",
            f"{body_indent}return\n",
            "\n",
        ]
        s[j:j] = block
        changed = True
        break  # one fix is enough; avoids multiple inserts

# --- Patch B: Make universe safe (prevents NoneType iterable in tick/scan) ---
# We insert a tiny helper + force universe to list near the top of tick().

txt = "".join(s)

# If helper already exists, skip
if "_ensure_list_universe(" not in txt:
    # Insert helper near imports (after "import config as CFG" if present)
    insert_at = 0
    m = re.search(r'^(import\s+config\s+as\s+CFG\s*)$', txt, flags=re.M)
    if m:
        # find line index of that import
        import_line = m.group(1)
        for i,l in enumerate(s):
            if l.strip() == import_line.strip():
                insert_at = i + 1
                break

    helper = [
        "\n",
        "def _ensure_list_universe(universe):\n",
        "    # Prevent: 'NoneType' object is not iterable\n",
        "    if universe is None:\n",
        "        return []\n",
        "    if isinstance(universe, (list, tuple)):\n",
        "        return list(universe)\n",
        "    # If someone stored a single symbol as string/object\n",
        "    return [universe]\n",
        "\n",
    ]
    s[insert_at:insert_at] = helper
    txt = "".join(s)
    changed = True

# Now force universe = _ensure_list_universe(universe) inside tick()
# We try to locate "def tick(" and inject after it, before any universe iteration.
s = txt.splitlines(True)

tick_idx = None
for i,l in enumerate(s):
    if re.match(r'^\s*def\s+tick\s*\(\s*\)\s*:\s*$', l):
        tick_idx = i
        break

if tick_idx is not None:
    tick_indent = indent_of(s[tick_idx]) + "    "
    # inject only if not already injected
    window = "".join(s[tick_idx: tick_idx+60])
    if "_ensure_list_universe(" not in window:
        inject = [
            f"{tick_indent}# --- SAFETY: keep universe iterable ---\n",
            f"{tick_indent}try:\n",
            f"{tick_indent}    universe = locals().get('universe', None)\n",
            f"{tick_indent}    universe = _ensure_list_universe(universe)\n",
            f"{tick_indent}    locals()['universe'] = universe\n",
            f"{tick_indent}except Exception:\n",
            f"{tick_indent}    universe = []\n",
            f"{tick_indent}    locals()['universe'] = universe\n",
            "\n",
        ]
        s[tick_idx+1:tick_idx+1] = inject
        changed = True

p.write_text("".join(s), encoding="utf-8")

if changed:
    print("PATCH_APPLIED: trading_cycle.py updated")
else:
    print("NO_CHANGE: nothing to patch (already fixed?)")
PY

echo "Sanity check (compile)..."
./venv/bin/python -m py_compile "$FILE"

echo "Restarting service..."
sudo systemctl restart trident

echo "Done ✅"
echo "Watch logs:"
echo "  sudo journalctl -u trident -f -o cat | egrep \"SCAN|TICK|ENGINE|ERROR|WARN\""
