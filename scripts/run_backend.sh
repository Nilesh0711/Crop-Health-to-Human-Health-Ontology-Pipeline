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
export OPENCODE_RESPONSE_FORMAT="${OPENCODE_RESPONSE_FORMAT:-model_default}"
export JSON_REPAIR_ATTEMPTS="${JSON_REPAIR_ATTEMPTS:-1}"
export JSON_REGENERATION_ATTEMPTS="${JSON_REGENERATION_ATTEMPTS:-1}"
export REGENERATION_MAX_OUTPUT_TOKENS="${REGENERATION_MAX_OUTPUT_TOKENS:-32768}"
export EXTRACTION_TIMEOUT_SECONDS="${EXTRACTION_TIMEOUT_SECONDS:-420}"
export EXTERNAL_TIMEOUT_SECONDS="${EXTERNAL_TIMEOUT_SECONDS:-1200}"
export EXTERNAL_JUDGEMENT_MODE="${EXTERNAL_JUDGEMENT_MODE:-per_paper_synthesis}"
export KNOWLEDGE_BUILDING_MODE="${KNOWLEDGE_BUILDING_MODE:-trust_graph}"
export TRUST_BATCH_SIZE="${TRUST_BATCH_SIZE:-3}"
export EXTERNAL_PAPER_WORKERS="${EXTERNAL_PAPER_WORKERS:-3}"
export OPEN_CODE_EXTERNAL_PAPER_MODEL="${OPEN_CODE_EXTERNAL_PAPER_MODEL:-opencode-go/deepseek-v4.1-flash}"
export EXTRACTION_MODE="${EXTRACTION_MODE:-target_context}"
export EXTRACTION_TARGET_WORDS="${EXTRACTION_TARGET_WORDS:-800}"
export EXTRACTION_CLAIMS_PER_PACKET="${EXTRACTION_CLAIMS_PER_PACKET:-0}"
export EXTRACTION_MIN_OUTPUT_BUDGET="${EXTRACTION_MIN_OUTPUT_BUDGET:-24576}"
export EXTERNAL_MIN_OUTPUT_BUDGET="${EXTERNAL_MIN_OUTPUT_BUDGET:-28192}"
# Fail early for stale exports instead of invoking an unapproved paid Zen model.
"$PYTHON" -c 'from backend.config.settings import Settings; Settings()'
export VALIDATION_BATCH_SIZE="${VALIDATION_BATCH_SIZE:-3}"
export QUERY_BATCH_SIZE="${QUERY_BATCH_SIZE:-6}"
export RETRIEVAL_WORKERS="${RETRIEVAL_WORKERS:-4}"
export ENABLE_NLI_PROMOTION="${ENABLE_NLI_PROMOTION:-0}"
"$PYTHON" -c 'from backend.providers.credentials import api_key; raise SystemExit(0 if api_key() else "Configure OPENCODE_API_KEY or run python -m backend.cli.configure_api_key on Windows.")'
echo "Direct OpenCode inference API enabled; credentials remain in the backend."
echo "Response format: $OPENCODE_RESPONSE_FORMAT; bounded format-repair attempts: $JSON_REPAIR_ATTEMPTS"
echo "Incomplete-response regeneration: $JSON_REGENERATION_ATTEMPTS; one extra recovery call total; no local JSON corrections."
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-BAAI/bge-base-en-v1.5}"
export EMBEDDING_QUERY_INSTRUCTION="${EMBEDDING_QUERY_INSTRUCTION:-Represent this sentence for searching relevant passages: }"
export CROSS_ENCODER_MODEL="${CROSS_ENCODER_MODEL:-ncbi/MedCPT-Cross-Encoder}"
export EMBEDDING_BATCH_SIZE="${EMBEDDING_BATCH_SIZE:-16}"
export CROSS_ENCODER_BATCH_SIZE="${CROSS_ENCODER_BATCH_SIZE:-8}"
export EXTERNAL_PREFETCH="${EXTERNAL_PREFETCH:-1}"
export CROSS_ENCODER_CANDIDATES="${CROSS_ENCODER_CANDIDATES:-20}"
export NLI_MODEL="${NLI_MODEL:-cross-encoder/nli-deberta-v3-base}"
export ENABLE_NLI_VALIDATOR="${ENABLE_NLI_VALIDATOR:-1}"

# Use CUDA for local specialist models when the installed torch wheel exposes it.
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
"$PYTHON" -c 'from backend.config.settings import Settings; s = Settings(); [print(f"LLM {task}: {model}") for task, model in s.task_models.items()]'
echo "Model policy: assigned primary only; model errors stop the run and appear in the activity log."
echo "Extraction timeout: ${EXTRACTION_TIMEOUT_SECONDS}s; minimum output budget: $EXTRACTION_MIN_OUTPUT_BUDGET"
echo "Extraction mode: $EXTRACTION_MODE; target size: $EXTRACTION_TARGET_WORDS words (configurable per run)"
echo "Extraction claim cap: $EXTRACTION_CLAIMS_PER_PACKET (0 = all eligible claims)"
echo "External request timeout: ${EXTERNAL_TIMEOUT_SECONDS}s"
echo "Web run default: knowledge building=$KNOWLEDGE_BUILDING_MODE; shared claim batch=$TRUST_BATCH_SIZE"
echo "Trust graph paper requests: up to $EXTERNAL_PAPER_WORKERS in parallel (EXTERNAL_PAPER_WORKERS=1 for serial execution)"
echo "External minimum generation budget (answer + reasoning): $EXTERNAL_MIN_OUTPUT_BUDGET tokens"
echo "Batch sizes: validation=$VALIDATION_BATCH_SIZE, query=$QUERY_BATCH_SIZE; retrieval workers=$RETRIEVAL_WORKERS"
echo "NLI automatic promotion enabled=$ENABLE_NLI_PROMOTION (requires admissible cited evidence)"
echo "Specialist-model device selection: $SPECIALIST_DEVICE_NOTE"
echo "Dense retriever: $EMBEDDING_MODEL ($EMBEDDING_DEVICE)"
echo "Cross-encoder reranker: $CROSS_ENCODER_MODEL ($CROSS_ENCODER_DEVICE)"
echo "NLI conflict detector: $NLI_MODEL ($NLI_DEVICE), enabled=$ENABLE_NLI_VALIDATOR"
echo "External judgement mode: $EXTERNAL_JUDGEMENT_MODE; per-paper model=$OPEN_CODE_EXTERNAL_PAPER_MODEL; synthesis model=${OPEN_CODE_EXTERNAL_MODEL:-opencode-go/mimo-v2.6-pro}; shared external Thinking; next-claim retrieval lookahead=$EXTERNAL_PREFETCH"
echo "Local inference batches: dense=$EMBEDDING_BATCH_SIZE, cross-encoder=$CROSS_ENCODER_BATCH_SIZE"
if [[ "${BACKEND_RELOAD:-0}" == "1" ]]; then
  echo "Development reload enabled: editing Python files interrupts active runs."
  exec "$PYTHON" -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
fi
echo "Automatic reload disabled so code edits do not interrupt active runs."
exec "$PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
