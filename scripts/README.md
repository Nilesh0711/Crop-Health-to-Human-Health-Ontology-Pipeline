# Startup and maintenance scripts

Run from Git Bash; each launcher resolves the repository from its own location.

| Script | Use |
| --- | --- |
| `setup.sh` | Install runtime Python/frontend dependencies |
| `run_backend.sh` | Start FastAPI with the assigned primary models |
| `run_frontend.sh` | Start the Vite development frontend |
| `enable_gpu_models.sh` | Configure GPU support for specialist models |
| `extraction_audit.py` | Snapshot/compare extraction results; writes developer reports under `reports/` by default |
| [`research/packet_comparison/`](research/packet_comparison/README.md) | Paired real-provider extraction experiments, source audits and reproducible reports |
| [`research/reasoning_comparison.py`](research/reasoning_comparison.py) | Isolated DeepSeek reasoning-mode pilot with source audits; leaves production settings unchanged |

The backend launcher disables automatic reload so edits do not terminate long
pipeline runs. For development only, use `BACKEND_RELOAD=1 bash scripts/run_backend.sh`.
Run one backend worker process for the local job registry.

Local generative-model launchers are removed. Install test dependencies using
`python -m pip install -r requirements-dev.txt`. Offline evaluation lives at
`python -m backend.cli.evaluation`; standalone external validation remains at
`python -m backend.cli.external_validation`.
