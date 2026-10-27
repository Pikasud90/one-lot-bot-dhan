#!/usr/bin/env bash
# Run a Backtest Lab backtest from the shell (the web app does the same from the Lab tab).
#   scripts/backtest_mac.sh --strategy ema_cross --instrument BANKNIFTY_OPT --timeframe 12m
# The synthetic-tape replay of the LIVE engine is a different tool:
#   .venv/bin/python -m onelot.replay --sessions 60
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "No .venv yet — run scripts/start_mac.sh once first." >&2
  exit 1
fi
exec "$ROOT/.venv/bin/python" -m onelot backtest "$@"
