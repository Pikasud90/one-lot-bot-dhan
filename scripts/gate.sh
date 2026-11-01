#!/usr/bin/env bash
# Quality gate: the exact checks CONTRIBUTING.md lists; CI will run the same script (US-008).
# Stops at the first failing step with a non-zero exit. Needs nothing outside this repo and
# its venv.
#   scripts/gate.sh
# pyproject.toml already deselects live tests; the gate says so again so that editing
# pytest's options there can never make the gate need broker credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  PY="python3" # stock macOS and Debian ship no bare `python`
fi
trap 'echo "gate: FAIL (exit $?) at: $BASH_COMMAND" >&2' ERR
echo "gate: $PY in $ROOT"
# Tool versions first, so a wrong interpreter is obvious before any step runs.
"$PY" -m ruff --version
"$PY" -m mypy --version

# --- steps: CONTRIBUTING.md lists exactly these ---
"$PY" -m ruff format --check .
"$PY" -m ruff check .
"$PY" scripts/mypy_ratchet.py
"$PY" -m pytest -m "not live"

echo "gate: PASS"
