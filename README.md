# Crop Health Research Workbench

A local-first application for extracting soil-to-human-health claims from research PDFs, checking source fidelity and independent external research support, and inspecting an evidence-aware knowledge graph. The bounded extractor emits up to four provenance-anchored claims per packet by default. OpenCode LongCat performs extraction; independent OpenCode Step 5 performs structured evidence judgements. BGE-base, BM25, MedCPT, and an NLI conflict detector support retrieval and validation. A supported graph edge is still a provisional research finding, not established causality.

## Start from Git Bash

Requirements: Python 3.11+, Node.js 20+, OpenCode authenticated with `opencode/longcat-2.5-preview-free` and `opencode/step-5-preview-free`, and internet access for Europe PMC, Crossref, OpenAlex, and first-use Hugging Face model downloads. Ollama is not required by the supported pipeline.

Run setup once:

```bash
bash scripts/setup.sh
```

Then keep two Git Bash terminals open:

```bash
bash scripts/run_backend.sh
```

```bash
bash scripts/run_frontend.sh
```

Open `http://127.0.0.1:5173`, select a PDF, inspect **Run settings**, and click **Run pipeline**. The UI shows live activity, stage decisions, evidence, graph views, and audit outcomes. LongCat is the fixed extractor and Step 5 is the fixed, independent evidence judge; only run limits are configurable in the UI.

The first validation run may take longer while these CPU models download and initialize:

- Dense retriever: `BAAI/bge-base-en-v1.5`
- Biomedical cross-encoder: `ncbi/MedCPT-Cross-Encoder`
- NLI conflict detector: `cross-encoder/nli-deberta-v3-base`

Override their model names, devices, or the NLI toggle with the environment variables shown in `scripts/run_backend.sh`. Keeping these specialist models on CPU avoids any local generative-model GPU requirement, but requires several GB of system RAM.

By default `run_backend.sh` places BGE, MedCPT, and DeBERTa NLI on `cuda:0` when CUDA-enabled PyTorch is installed; otherwise it keeps them on CPU and prints an actionable message. On this Windows machine, run the one-time installer below, then restart the backend:

```bash
bash scripts/enable_gpu_models.sh
```

For one-server use, run `npm.cmd run build` inside `frontend/`, then start FastAPI; it serves the built UI at `http://127.0.0.1:8000`.

## Pipeline

1. Extract section-aware packets of about 4,000 words from the source PDF, with a 500-word overlap. Preserve author-year citations across `et al.`, retain conservative unnumbered topic headings, and resolve comma-free or compact citations such as `Ding et al. 1997` and `Barnwal et al.2013` against APA bibliography entries.
2. Rank likely mechanistic sentences as an attention index, then ask OpenCode LongCat for a bounded number of atomic claims using the full rich packet, exact one-to-three-sentence evidence spans, and distinct noun-phrase entity anchors that exclude the relation trigger.
3. Reject malformed claims and relations outside the automatic relation policy. Explicit causal predicates are deferred to a stricter causal workflow.
4. Run one Step 5 semantic scope and canonical entity-type check. A classifier type disagreement is audited when the extracted types already form a legal edge; the source gate checks the proposed relation and direction.
5. Normalize only high-confidence local vocabulary matches; preserve original labels and never invent ontology IDs.
6. Validate source fidelity with a separate stateless Step 5 call plus deterministic anchor and modality checks. A generic disease mention may use an exact local section heading as a bounded antecedent; distant context cannot prove the claim. Explicit hedges such as `may`, `can`, and `could` remain conditional.
7. Resolve numeric and author-year source citations, then search independent literature across Europe PMC and OpenAlex. Each claim gets one planned retrieval pass: a literal concept query plus one bounded measurement query (for example, association -> incidence/severity). The retrieval sentence is derived from the claim without another LLM call. A cited lead and the independent-paper budget are selected separately. Rank metadata using standard BM25/BGE-base reciprocal-rank fusion, then rerank the leading candidates with MedCPT.
8. Prefer Europe PMC full-text XML; fall back to an explicit OpenAlex open-access PDF and then an indexed abstract. External files are processed in memory, and only selected page-labelled passages are stored.
9. Judge each external document separately. When a retrieved chunk contains an exact sentence naming both endpoints and a compatible relation cue, focus Step 5 on that sentence so a related pathogen or mechanism cannot replace the claim-level subject. DeBERTa NLI can veto strong contradiction in external evidence. Source-reference support and independent corroboration are counted separately; only independently supported claims enter the main graph.

The starter automatic graph is restricted to direct pathway/material relations: `affects`, `increases`, `reduces`, `has_nutrient`, `has_contaminant`, `contains`, and `has_trait`. Association-only and comparative observations are retained for later observational-evidence modelling rather than being treated as universal graph edges.
10. Write a stratified evaluation sample with blank gold-label fields for independent review and measurable precision/recall.

Repeated claims from different source spans remain separate validation units. Only exact overlapping duplicate occurrences are collapsed. This prevents a good occurrence from hiding a weak or incorrectly typed occurrence.

## Data and code

| Location | Purpose |
| --- | --- |
| `pdf/` | Source PDFs; never removed by the pipeline |
| `backend/app.py` | FastAPI PDF, run, log, claim, graph, and upload endpoints |
| `backend/pipeline.py` | PDF packets, OpenCode extraction, gating, and orchestration |
| `backend/ontology_schema.py` | Stable type/relation vocabulary and pathway-shape policy |
| `backend/semantic_validation.py` | One Step 5 scope and canonical entity-type check |
| `backend/source_validation.py` | Exact-span source entailment, relation, direction, and modality checks |
| `backend/vocabulary.py` | Conservative local ChEBI/ontology normalization |
| `backend/evaluation.py` | Stratified evaluation sample and metric calculation |
| `backend/graph.py` | Evidence-state graph snapshots and provisional paths |
| `external_validator/` | OpenCode adapter, scholarly retrieval, hybrid ranking, cross-encoder reranking, NLI, and Step 5 judgement |
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
