# Startup and maintenance scripts

Run from Git Bash; each launcher resolves the repository from its own location.

| Script | Use |
| --- | --- |
| `setup.sh` | Install runtime Python/frontend dependencies |
| `run_backend.sh` | Start FastAPI on port 8000 with automatic backend code/prompt reload |
| `run_frontend.sh` | Start the Vite development frontend |
| `enable_gpu_models.sh` | Configure GPU support for specialist models |

Run `setup.sh` when installing the project or updating dependencies, rather than
before each run. On a machine compatible with the script's CUDA PyTorch wheel,
`enable_gpu_models.sh` is an optional one-time environment setup for BGE/MedCPT;
it does not start a model server or need to precede every launch. The backend
launcher detects available CUDA and otherwise uses CPU. Explicit device settings
remain authoritative. Start only `run_backend.sh` and `run_frontend.sh` for normal
development use.

The backend launcher enables automatic reload by default. It watches only
`backend/` Python and Markdown prompt files, so generated results, PDFs and
archived code do not trigger reload. Editing watched files interrupts active
requests; compatible saved checkpoints remain resumable. Disable reload explicitly
with `BACKEND_RELOAD=0 bash scripts/run_backend.sh`. Run one backend worker process
for the local job registry. The frontend launcher retains Vite hot reload.

Local generative-model launchers are removed. Install test dependencies using
`python -m pip install -r requirements-dev.txt`. Offline evaluation lives at
`python -m backend.cli.evaluation`; standalone external validation remains at
`python -m backend.cli.external_validation`.

Results publication uses `python -m backend.cli.export_viewer`; see the
[read-only deployment guide](../docs/deployment/render_viewer.md). It exports saved
findings without running inference. The local pipeline remains on port 8000;
the viewer preview uses port 8011.

Retired executable comparisons live under
[`archive/legacy_pipeline/`](../archive/legacy_pipeline/README.md). Frozen packet,
reasoning and scheduling experiments and the legacy extraction audit are in its
`research_tools/` directory;
they are not startup requirements. Research scripts that call real providers
remain separate from the offline test suite.
