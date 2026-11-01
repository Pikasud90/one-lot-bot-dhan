#!/usr/bin/env bash
# Quality gate: the exact checks CONTRIBUTING.md lists and CI runs. Stops at the first
# failing step with a non-zero exit. Needs nothing outside this repo and its venv.
#   scripts/gate.sh
# pytest takes its options from pyproject.toml (addopts: -m "not live").
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python"
fi
trap 'echo "gate: FAIL (exit $?) at: $BASH_COMMAND" >&2' ERR
echo "gate: $PY in $ROOT"

"$PY" -m ruff format --check .
"$PY" -m ruff check .
"$PY" scripts/mypy_ratchet.py
"$PY" -m pytest

echo "gate: PASS"
