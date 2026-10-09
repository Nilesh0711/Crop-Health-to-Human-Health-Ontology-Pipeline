#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [[ -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "Python was not found. Create the project .venv and install requirements.txt." >&2
  exit 1
fi

cd "$PROJECT_ROOT"
export OLLAMA_CONTEXT_TOKENS="${OLLAMA_CONTEXT_TOKENS:-8192}"
echo "Starting FastAPI at http://127.0.0.1:8000"
echo "Ollama request context: $OLLAMA_CONTEXT_TOKENS tokens"
exec "$PYTHON" -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
