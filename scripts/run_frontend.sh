#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

if command -v npm >/dev/null 2>&1; then
  NPM="$(command -v npm)"
elif command -v npm.cmd >/dev/null 2>&1; then
  NPM="$(command -v npm.cmd)"
else
  echo "npm was not found. Install Node.js 20+ and ensure npm is on PATH." >&2
  exit 1
fi

cd "$FRONTEND_DIR"
if [[ ! -d node_modules ]]; then
  echo "Frontend packages are not installed; running npm install."
  "$NPM" install
fi

echo "Starting React/Vite at http://127.0.0.1:5173"
exec "$NPM" run dev
