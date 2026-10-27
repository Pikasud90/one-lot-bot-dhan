#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$ROOT/data/engine.pid" ]]; then
  PID="$(cat "$ROOT/data/engine.pid")"
  kill "$PID" 2>/dev/null && echo "stopped engine (PID $PID)" || true
else
  pkill -f "python -m onelot" 2>/dev/null && echo "stopped engine" || echo "engine not running"
fi
