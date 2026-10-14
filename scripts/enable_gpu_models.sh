#!/usr/bin/env bash
set -euo pipefail

# Install the CUDA PyTorch wheel required by BGE, MedCPT, and DeBERTa NLI.
# The NVIDIA driver supplies CUDA 13.4; PyTorch's CUDA 13.2 wheel is supported
# by that newer driver. This wheel is approximately 2 GB and is a one-time
# download. It does not install or start any local generative LLM.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [[ -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
else
  echo "Python virtual environment not found. Run bash scripts/setup.sh first." >&2
  exit 1
fi

echo "Installing CUDA-enabled PyTorch 2.14.1 (CUDA 13.2 wheel). This is about 2 GB."
"$PYTHON" -m pip install --force-reinstall torch==2.14.1+cu132 \
  --index-url https://download.pytorch.org/whl/cu132

"$PYTHON" - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit("PyTorch installed but CUDA is still unavailable. Check the NVIDIA driver, then restart Git Bash.")
print(f"CUDA ready: {torch.cuda.get_device_name(0)} | torch={torch.__version__} | CUDA={torch.version.cuda}")
PY

echo "Done. Restart the backend; run_backend.sh will automatically place BGE, MedCPT, and NLI on cuda:0."
