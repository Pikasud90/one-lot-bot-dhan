#!/usr/bin/env bash
# Bank Nifty Trade Assistant — macOS / Linux launcher.
#
# Every path is quoted: this project's own directory contains spaces, and an
# unquoted variable would silently break the venv and the database path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="$ROOT/.venv"
PORT="${ONELOT_PORT:-8787}"
ARGS=()
REINSTALL=0

usage() {
  cat <<'USAGE'
Usage: scripts/start_mac.sh [options]
  --mock         run against the synthetic market (no credentials needed)
  --port N       API/dashboard port (default 8787)
  --reinstall    rebuild the virtual environment from scratch
  -h, --help     this message
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mock)      ARGS+=("--mock"); shift ;;
    --port)      PORT="$2"; shift 2 ;;
    --reinstall) REINSTALL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           ARGS+=("$1"); shift ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.10+ and retry." >&2
  exit 1
fi

if [[ "$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')" != "1" ]]; then
  echo "ERROR: Python 3.10 or newer is required." >&2
  "$PYTHON_BIN" --version >&2
  exit 1
fi

if [[ "$REINSTALL" == "1" ]]; then rm -rf "$VENV"; fi

if [[ ! -d "$VENV" ]]; then
  echo "==> creating virtual environment at $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  echo "==> installing dependencies (first run only, about a minute)"
  "$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
  # Editable install so `python -m onelot` resolves without PYTHONPATH games.
  "$VENV/bin/python" -m pip install --quiet -e "$ROOT"
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "WARNING: no .env found."
  echo "         cp .env.example .env  and fill in your Dhan credentials,"
  echo "         or run with --mock to use the synthetic market."
fi

mkdir -p "$ROOT/logs" "$ROOT/data"

echo "==> starting the engine  (dashboard: http://127.0.0.1:$PORT/)"
echo "    press Ctrl-C to stop"
exec "$VENV/bin/python" -m onelot --port "$PORT" ${ARGS[@]+"${ARGS[@]}"}
