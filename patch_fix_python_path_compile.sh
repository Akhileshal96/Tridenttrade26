#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/trident-bot"
cd "$ROOT"

echo "== Finding python =="
PY=""

# Prefer venv if present (common names)
for cand in \
  "$ROOT/venv/bin/python" \
  "$ROOT/.venv/bin/python" \
  "$ROOT/env/bin/python" \
  "/home/ubuntu/venv/bin/python" \
  "$(command -v python3)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "❌ Could not find python interpreter"
  exit 1
fi

echo "✅ Using: $PY"

echo "== Compile sanity =="
"$PY" -m py_compile trading_cycle.py
"$PY" -m py_compile bot.py

echo "== Restart service =="
sudo systemctl restart trident

echo "DONE ✅ compile+restart complete"
