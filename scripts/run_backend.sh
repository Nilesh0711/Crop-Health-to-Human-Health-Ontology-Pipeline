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
if ! command -v opencode >/dev/null 2>&1; then
  echo "OpenCode was not found on PATH. Install it and authenticate before starting the backend." >&2
  exit 1
fi
export EXTRACTION_MODEL="${EXTRACTION_MODEL:-opencode/longcat-2.5-preview-free}"
export OPEN_CODE_VALIDATION_MODEL="${OPEN_CODE_VALIDATION_MODEL:-opencode/step-5-preview-free}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-BAAI/bge-base-en-v1.5}"
export EMBEDDING_QUERY_INSTRUCTION="${EMBEDDING_QUERY_INSTRUCTION:-Represent this sentence for searching relevant passages: }"
export CROSS_ENCODER_MODEL="${CROSS_ENCODER_MODEL:-ncbi/MedCPT-Cross-Encoder}"
export CROSS_ENCODER_CANDIDATES="${CROSS_ENCODER_CANDIDATES:-20}"
export NLI_MODEL="${NLI_MODEL:-cross-encoder/nli-deberta-v3-base}"
export ENABLE_NLI_VALIDATOR="${ENABLE_NLI_VALIDATOR:-1}"
export EXTERNAL_JUDGE_WORKERS="${EXTERNAL_JUDGE_WORKERS:-3}"

# Prefer the free GPU for local specialist models. The current CPU-only torch
# wheel cannot expose CUDA, so retain a safe CPU fallback until
# enable_gpu_models.sh has installed the CUDA-enabled wheel.
if "$PYTHON" -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"; then
  DEFAULT_SPECIALIST_DEVICE="cuda:0"
  SPECIALIST_DEVICE_NOTE="CUDA available"
else
  DEFAULT_SPECIALIST_DEVICE="cpu"
  SPECIALIST_DEVICE_NOTE="CUDA unavailable; run bash scripts/enable_gpu_models.sh once"
fi
export EMBEDDING_DEVICE="${EMBEDDING_DEVICE:-$DEFAULT_SPECIALIST_DEVICE}"
export CROSS_ENCODER_DEVICE="${CROSS_ENCODER_DEVICE:-$DEFAULT_SPECIALIST_DEVICE}"
export NLI_DEVICE="${NLI_DEVICE:-$DEFAULT_SPECIALIST_DEVICE}"
echo "Starting FastAPI at http://127.0.0.1:8000"
echo "OpenCode extraction model: $EXTRACTION_MODEL (~4,000-word packets)"
echo "OpenCode evidence-validation model: $OPEN_CODE_VALIDATION_MODEL"
echo "No Ollama model is required by this pipeline."
echo "Specialist-model device selection: $SPECIALIST_DEVICE_NOTE"
echo "Dense retriever: $EMBEDDING_MODEL ($EMBEDDING_DEVICE)"
echo "Cross-encoder reranker: $CROSS_ENCODER_MODEL ($CROSS_ENCODER_DEVICE)"
echo "NLI conflict detector: $NLI_MODEL ($NLI_DEVICE), enabled=$ENABLE_NLI_VALIDATOR"
echo "External evidence judgements: up to $EXTERNAL_JUDGE_WORKERS parallel Step 5 requests per claim"
exec "$PYTHON" -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
