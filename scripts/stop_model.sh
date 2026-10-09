#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${VALIDATION_MODEL:-qwen3:8b}}"

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama was not found on PATH." >&2
  exit 1
fi

if ! curl --silent --show-error --fail http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Ollama is not running at http://127.0.0.1:11434"
  exit 0
fi

echo "Unloading $MODEL from memory. The Ollama server will remain running."
ollama stop "$MODEL"
