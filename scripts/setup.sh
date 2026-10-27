#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if command -v python3 >/dev/null 2>&1; then
  BASE_PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  BASE_PYTHON="$(command -v python)"
else
  echo "Python 3.11+ was not found on PATH." >&2
  exit 1
fi

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [[ -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
else
  echo "Creating project virtual environment."
  "$BASE_PYTHON" -m venv "$PROJECT_ROOT/.venv"
  if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
  else
    PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
  fi
fi

"$PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"

if command -v npm >/dev/null 2>&1; then
  NPM="$(command -v npm)"
elif command -v npm.cmd >/dev/null 2>&1; then
  NPM="$(command -v npm.cmd)"
else
  echo "npm was not found. Install Node.js 20+ before starting the frontend." >&2
  exit 1
fi

cd "$PROJECT_ROOT/frontend"
"$NPM" install
echo "Setup complete. Configure the backend OpenCode API key, then start the backend and frontend in separate terminals."
