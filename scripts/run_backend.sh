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
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-BAAI/bge-large-en-v1.5}"
export EMBEDDING_DEVICE="${EMBEDDING_DEVICE:-cpu}"
export EMBEDDING_QUERY_INSTRUCTION="${EMBEDDING_QUERY_INSTRUCTION:-Represent this sentence for searching relevant passages: }"
export CROSS_ENCODER_MODEL="${CROSS_ENCODER_MODEL:-ncbi/MedCPT-Cross-Encoder}"
export CROSS_ENCODER_DEVICE="${CROSS_ENCODER_DEVICE:-cpu}"
export CROSS_ENCODER_CANDIDATES="${CROSS_ENCODER_CANDIDATES:-20}"
export NLI_MODEL="${NLI_MODEL:-cross-encoder/nli-deberta-v3-large}"
export NLI_DEVICE="${NLI_DEVICE:-cpu}"
export ENABLE_NLI_VALIDATOR="${ENABLE_NLI_VALIDATOR:-1}"
echo "Starting FastAPI at http://127.0.0.1:8000"
echo "Ollama request context: $OLLAMA_CONTEXT_TOKENS tokens"
echo "Dense retriever: $EMBEDDING_MODEL ($EMBEDDING_DEVICE)"
echo "Cross-encoder reranker: $CROSS_ENCODER_MODEL ($CROSS_ENCODER_DEVICE)"
echo "NLI conflict detector: $NLI_MODEL ($NLI_DEVICE), enabled=$ENABLE_NLI_VALIDATOR"
exec "$PYTHON" -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
