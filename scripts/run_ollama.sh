#!/usr/bin/env bash
set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
MODEL="${1:-${VALIDATION_MODEL:-qwen3:8b}}"
# Windows/WDDM already holds roughly 0.8-1.1 GiB of the RTX 5060's VRAM.
# Leave Ollama's extra reservation at zero so it can fully offload the smaller
# Qwen 8B model and use the remaining VRAM for its KV cache.  This still leaves
# about the requested 800 MiB available to Windows.
# Override with OLLAMA_GPU_OVERHEAD if a larger reserve is needed.
export OLLAMA_GPU_OVERHEAD="${OLLAMA_GPU_OVERHEAD:-0}"
SERVER_PID=""
SERVER_OWNED=false
LOG_TAIL_PID=""

echo "============================================================"
echo " Crop Health Workbench - Ollama service"
echo "============================================================"
echo "Endpoint: $OLLAMA_HOST"
echo "Default/configured model: $MODEL"
echo "Ollama extra GPU reserve: $OLLAMA_GPU_OVERHEAD bytes (default 0; Windows keeps its own VRAM allocation)"
echo

if ! command -v ollama >/dev/null 2>&1; then
  echo "ERROR: Ollama was not found on PATH."
  echo "Install Ollama, restart Git Bash, and run this script again."
  exit 1
fi

if curl --silent --show-error --fail "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
  echo "Ollama is already running. No second server will be started."
  echo "NOTE: The running server does not inherit this script's GPU reserve setting."
  echo "To apply it, quit Ollama from the Windows system tray, then rerun this script."
  echo
  echo "Installed local models:"
  ollama list
  if ! ollama list | awk 'NR > 1 { print $1 }' | grep -Fxq -- "$MODEL"; then
    echo
    echo "NOTE: $MODEL is not installed yet. To install it, run:"
    echo "  ollama pull $MODEL"
  fi
  echo
  echo "Following the active Ollama server log and watching loaded models."
  echo "Press Ctrl+C to unload $MODEL. The existing Ollama server will remain running."
  if [[ -n "${LOCALAPPDATA:-}" ]] && command -v cygpath >/dev/null 2>&1; then
    LOG_DIR="$(cygpath -u "$LOCALAPPDATA")/Ollama"
    LOG_FILE=""
    while IFS= read -r candidate; do
      if [[ -s "$candidate" ]]; then
        LOG_FILE="$candidate"
        break
      fi
    done < <(ls -t "$LOG_DIR"/server*.log 2>/dev/null || true)
    if [[ -n "$LOG_FILE" ]]; then
      echo "Live server log: $LOG_FILE"
      tail -n 12 -F "$LOG_FILE" &
      LOG_TAIL_PID=$!
    else
      echo "No non-empty server log file was found under $LOG_DIR."
    fi
  else
    echo "Could not locate the Windows Ollama log directory; use the terminal that started Ollama to see server logs."
  fi
  echo
else
  echo "Ollama is not responding. Starting its local API server."
  echo "Ollama will use the configured GPU reserve for model placement."
  echo "Server logs will stream below. Keep this terminal open while using the workbench."
  echo "The selected model loads when the frontend/backend starts a pipeline run."
  echo "Press Ctrl+C to unload $MODEL and stop this Ollama server."
  echo
  ollama serve &
  SERVER_PID=$!
  SERVER_OWNED=true
fi

cleanup() {
  trap - INT TERM
  echo
  if [[ -n "$LOG_TAIL_PID" ]]; then
    kill "$LOG_TAIL_PID" 2>/dev/null || true
    wait "$LOG_TAIL_PID" 2>/dev/null || true
  fi
  echo "Stopping model $MODEL..."
  if ! ollama stop "$MODEL"; then
    echo "Model $MODEL was not loaded, or Ollama could not unload it."
  fi

  if [[ "$SERVER_OWNED" == true && -n "$SERVER_PID" ]]; then
    echo "Stopping the Ollama server started by this script..."
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  else
    echo "The pre-existing Ollama server will keep running."
  fi
  echo "Cleanup complete."
  exit 0
}

trap cleanup INT TERM

LAST_PS=""
while true; do
  if [[ "$SERVER_OWNED" == true ]] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" || true
    echo "Ollama server stopped unexpectedly."
    exit 1
  fi

  CURRENT_PS="$(ollama ps 2>&1 || true)"
  if [[ "$CURRENT_PS" != "$LAST_PS" ]]; then
    echo "--- Loaded model status ---"
    printf '%s\n' "$CURRENT_PS"
    echo "---------------------------"
    LAST_PS="$CURRENT_PS"
  fi
  sleep 2
done
