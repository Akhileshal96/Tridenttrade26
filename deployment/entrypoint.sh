#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-both}"

run_research() {
  echo "[entrypoint] running nightly research"
  python -m zerodha_bot.night_research
}

run_trade() {
  echo "[entrypoint] running trading loop"
  args=("--poll-seconds" "${POLL_SECONDS:-30}")
  if [[ -n "${MAX_CYCLES:-}" ]]; then
    args+=("--max-cycles" "${MAX_CYCLES}")
  fi
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    args+=("--dry-run")
  fi
  python -m zerodha_bot.trading_bot "${args[@]}"
}

case "$MODE" in
  research)
    run_research
    ;;
  trade)
    run_trade
    ;;
  both)
    run_research
    run_trade
    ;;
  *)
    echo "Unknown mode: $MODE (expected: research|trade|both)" >&2
    exit 2
    ;;
esac
