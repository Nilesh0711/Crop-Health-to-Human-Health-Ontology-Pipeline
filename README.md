# Crop Health Research Workbench

A local-first application for extracting soil-to-human-health claims from research PDFs, checking source fidelity and independent external research support, and inspecting an evidence-aware knowledge graph. The bounded extractor emits up to four provenance-anchored claims per packet by default. Local Qwen performs extraction and structured judgements; BGE-base, BM25, MedCPT, and an NLI conflict detector support retrieval and validation. A supported graph edge is still a provisional research finding, not established causality.

## Start from Git Bash

Requirements: Python 3.11+, Node.js 20+, Ollama with `qwen3:8b` (the default), and internet access for Europe PMC, Crossref, OpenAlex, and first-use Hugging Face model downloads.

Run setup once:

```bash
bash scripts/setup.sh
```

Then keep three Git Bash terminals open:

```bash
bash scripts/run_ollama.sh
```

```bash
bash scripts/run_backend.sh
```

```bash
bash scripts/run_frontend.sh
```

`run_ollama.sh` sets Ollama's extra GPU reserve to zero because Windows/WDDM already consumes part of the 8 GiB VRAM. Quit the Ollama tray application before starting the script if you want these settings to apply. Ctrl+C unloads the configured model and stops the server if the script started it.

Open `http://127.0.0.1:5173`, select a PDF, inspect **Run settings**, and click **Run pipeline**. The UI shows live activity, stage decisions, evidence, graph views, and audit outcomes. The default local model and number of extracted claims per packet can be changed per run.

The first validation run may take longer while these CPU models download and initialize:

- Dense retriever: `BAAI/bge-base-en-v1.5`
- Biomedical cross-encoder: `ncbi/MedCPT-Cross-Encoder`
- NLI conflict detector: `cross-encoder/nli-deberta-v3-base`

Override their model names, devices, or the NLI toggle with the environment variables shown in `scripts/run_backend.sh`. Keeping these models on CPU leaves GPU memory available for Qwen, but requires several GB of system RAM.

For one-server use, run `npm.cmd run build` inside `frontend/`, then start FastAPI; it serves the built UI at `http://127.0.0.1:8000`.

## Pipeline

1. Extract section-aware packets of about 2,000 words from the source PDF.
2. Rank likely mechanistic sentences as an attention index, then ask Qwen for a bounded number of atomic claims using the full ~2,000-word packet, exact one-to-three-sentence evidence spans, and exact entity/relation anchors.
3. Reject malformed claims and relations outside the automatic relation policy. Explicit causal predicates are deferred to a stricter causal workflow.
4. Run Qwen semantic scope, type, relation, direction, and atomicity validation.
5. Normalize only high-confidence local vocabulary matches; preserve original labels and never invent ontology IDs.
6. Validate source fidelity with a separate stateless Qwen call plus deterministic anchor and modality checks. Explicit hedges such as `may`, `can`, and `could` remain conditional. DeBERTa NLI may veto an apparent entailment when it detects strong contradiction, but cannot promote a claim.
7. Resolve numeric and author-year source citations, then search independent literature across Europe PMC and OpenAlex. Rank metadata using standard BM25/BGE-base reciprocal-rank fusion, then rerank the leading candidates with MedCPT.
8. Prefer Europe PMC full-text XML; fall back to an explicit OpenAlex open-access PDF and then an indexed abstract. External files are processed in memory, and only selected page-labelled passages are stored.
9. Judge each external document separately. Source-reference support and independent corroboration are counted separately; only independently supported claims enter the main graph.
10. Write a stratified evaluation sample with blank gold-label fields for independent review and measurable precision/recall.

Repeated claims from different source spans remain separate validation units. Only exact overlapping duplicate occurrences are collapsed. This prevents a good occurrence from hiding a weak or incorrectly typed occurrence.

## Data and code

| Location | Purpose |
| --- | --- |
| `pdf/` | Source PDFs; never removed by the pipeline |
| `backend/app.py` | FastAPI PDF, run, log, claim, graph, and upload endpoints |
| `backend/pipeline.py` | PDF packets, extraction, gating, and orchestration |
| `backend/ontology_schema.py` | Stable type/relation vocabulary and pathway-shape policy |
| `backend/semantic_validation.py` | Qwen scope, entity-type, relation, direction, and atomicity gate |
| `backend/source_validation.py` | Exact-span source entailment and NLI conflict checks |
| `backend/vocabulary.py` | Conservative local ChEBI/ontology normalization |
| `backend/evaluation.py` | Stratified evaluation sample and metric calculation |
| `backend/graph.py` | Evidence-state graph snapshots and provisional paths |
| `external_validator/` | Scholarly retrieval, hybrid ranking, cross-encoder reranking, NLI, and Qwen judgement |
| `frontend/` | React/Vite/Cytoscape application |
| `ontology/<PDF stem>/` | Per-PDF candidates, validation records, graph, logs, summary, and evaluation sample |

Whole external articles are never saved. The graph distinguishes independently corroborated, independently single-source, source-reference-only, contested, contradicted, and insufficient-evidence states. `external_evidence_not_found` means the bounded retrieval run found no acceptable evidence; it is not a false verdict.

## Evaluation

Each completed run creates `evaluation_sample.jsonl`. Fill its `gold` fields without changing the recorded prediction, then score it:

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Generate a fresh stratified sample if required:

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline sample "ontology/<PDF stem>" --limit 80
```

The standalone external validator remains available through `python -m external_validator.cli`; see [external_validator/README.md](external_validator/README.md) and [docs/CONTEXT.md](docs/CONTEXT.md).
