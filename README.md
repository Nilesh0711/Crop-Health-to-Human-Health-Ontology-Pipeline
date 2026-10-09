# Crop Health Research Workbench

A local-first application for extracting soil-to-human-health claims from research PDFs, checking source fidelity and external research support, and inspecting an evidence-aware knowledge graph. The validator uses Europe PMC open-access articles, `BAAI/bge-small-en-v1.5` for passage ranking, and local Qwen for extraction and evidence judgement. A corroborated graph edge is still a provisional research finding, not established causality.

## Start the application from Git Bash

Requirements: Python 3.11+, Node.js 20+, Ollama with `qwen3:8b` (the configured default) or another installed model, and internet access for Europe PMC. The 8B model is selected because it can be fully offloaded on this workstation's 8 GiB GPU. Run setup once from the project folder:

```bash
bash scripts/setup.sh
```

Then open three Git Bash terminals in the project folder and run one command in each:

```bash
bash scripts/run_ollama.sh
```

```bash
bash scripts/run_backend.sh
```

```bash
bash scripts/run_frontend.sh
```

Keep the three terminals open. `run_ollama.sh` leaves Ollama's extra GPU reserve at zero because Windows/WDDM already holds roughly 0.8–1.1 GiB of VRAM. This allows the 8B model and its 8,192-token context to remain GPU-resident while leaving Windows about the requested 800 MiB. To apply this setting, quit the Ollama app from the Windows system tray before starting the script; a server already running in the app cannot inherit the script's setting, and the script will tell you when that is the case. Pressing Ctrl+C unloads its configured model; if the script started Ollama, it also stops that server. The frontend script installs packages if `frontend\node_modules\` is missing.

In the frontend, open **Run settings** to choose an installed Ollama model for that pipeline run. The initial selection follows `VALIDATION_MODEL` (default `qwen3:8b`). The selected model is recorded in the run summary and used for extraction and external article judging. Check currently loaded models with `ollama ps`. To stop an Ollama server that was already running in the Windows app, close it from the system tray.

Open `http://127.0.0.1:5173`. Choose a PDF in the library, inspect run settings, and click **Run pipeline**. The default is all pages and all eligible claims; `0` means all. Validation can be slow because every claim involves local model calls and external article retrieval. The page shows logs, claims with source quotations and article decisions, a per-paper or merged graph, and provisional reasoning paths. You can add a PDF from the sidebar (50 MB upload limit).

For one-server use, run `npm.cmd run build` inside `frontend/`, then start FastAPI; it serves the built UI at `http://127.0.0.1:8000`.

## Data and code

| Location | Purpose |
| --- | --- |
| `pdf/` | Source PDFs; never removed by the pipeline |
| `backend/app.py` | FastAPI PDF, run, logs, claims, graph, and upload endpoints |
| `backend/pipeline.py` | Section-aware PDF packets, Qwen claim extraction, controlled-vocabulary checks, and validation orchestration |
| `backend/semantic_validation.py` | Independent Qwen scope, entity-type, relation, direction, and atomicity gate |
| `backend/source_validation.py` | Source-PDF entailment gate: exact 1–3 sentence evidence, entity/relation/direction/qualifier checks |
| `backend/graph.py` | Evidence-state graph snapshots and forward-only provisional paths |
| `external_validator/` | Europe PMC retrieval, BGE reranking, local Qwen judgement, CLI |
| `frontend/` | React/Vite/Cytoscape application |
| `ontology/<PDF stem>/` | Newly generated per-PDF candidate, validation, graph, log, and summary files |
| `archive/pre_app_2026-10-07/` | Old ontology and RAG data preserved before the new extraction run |

Only selected external article chunks and bibliographic metadata are persisted in `validation_runs.jsonl`; whole external articles are never saved. Extraction uses ~2,000-word packets, constrained JSON, and exact 1–3-sentence PDF evidence spans. Each claim passes separate Qwen entity typing, relation/scope classification, and source-evidence judgement. Only source-supported claims reach concept-only Europe PMC retrieval; the source-paper title is excluded, and BGE ranks articles before their transient full text is fetched. Rejected or uncertain claims remain in the audit view. External support requires a passage with the exact entities, direction, and certainty. A per-PDF GraphML export is downloadable. A graph path is a hypothesis, not a medical or agronomic conclusion.

Saved legacy runs were snapshotted in `analysis/extraction_baseline_2026-10-08.jsonl` before this change. To compare counts and claim overlap after rerunning PDFs, use `./.venv/Scripts/python.exe scripts/extraction_audit.py compare --baseline analysis/extraction_baseline_2026-10-08.jsonl`. These are process metrics, not measured precision: the baseline has no expert truth labels. The script can also create a new baseline with optional provisional Qwen review; see its `--help` output.

The standalone validator CLI remains available via `python -m external_validator.cli`; see [external_validator/README.md](external_validator/README.md) and [docs/CONTEXT.md](docs/CONTEXT.md) for context.
