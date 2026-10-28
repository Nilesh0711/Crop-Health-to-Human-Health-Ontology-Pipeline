# Crop Health Research Workbench

To share saved results without running models, use the existing UI's read-only
viewer and the prepared Render Free configuration. See the
[deployment guide](docs/deployment/render_viewer.md) for snapshot export, local
preview, deployment and updates.

A local-first application for extracting soil-to-human-health claims from research PDFs, checking source fidelity and external research support, and inspecting an evidence-aware knowledge graph. The bounded extractor emits up to four provenance-anchored claims per packet by default. OpenCode uses Go DeepSeek Flash for extraction/source fidelity, MiMo Flash for queries, and one joint MiMo Pro external judgement per claim. BGE-base, BM25, MedCPT, and an NLI conflict detector support retrieval and validation. A supported graph edge is still a provisional research finding, not established causality.

## Start from Git Bash

Requirements: Python 3.11+, Node.js 20+, an OpenCode API key with access to the selected Go models, and internet access for Europe PMC, Crossref, OpenAlex, and first-use Hugging Face model downloads. The default backend calls inference directly; OpenCode CLI and Ollama are not required.

v34 adds [chapter-aware parsing, citation tracking, request progress, quoted contextual type evidence and PDF page diagnostics](backend/README.md), with a 1200-second external timeout. The joint protocol from v33 removes standalone semantic and keyword-sampled scope model gates. The joint external judgement assesses evidence-grounded types and scope as well as support/conflict. Each task uses only its assigned primary model. Model errors stop the run and appear in the frontend activity log. There is no model switching or automatic provider-error retry. One bounded same-model recovery call may propose parsing edits or regenerate missing response content from the original evidence. No local JSON correction is performed; failed runs can resume saved work. See [task configuration](backend/stages/external_validation/README.md#models-and-error-behavior) and [the v33 protocol](backend/stages/external_validation/README.md). Restart the backend to load the policy if reload is disabled. Source failures and verified external contradictions offer a bounded manual repair; unfinished external checks offer a claim-only retry. Saved historical runs are not automatically rejudged.

Run setup once:

```bash
bash scripts/setup.sh
```

Configure `OPENCODE_API_KEY` in the backend environment, or use
`python -m backend.cli.configure_api_key` on Windows to store it in Credential
Manager. See [API formatting, bounded repair and resume](backend/providers/README.md).

Then keep two Git Bash terminals open:

Backend automatic reload is disabled during normal runs. Use `BACKEND_RELOAD=1`
only for development: reload interrupts active requests. Saved jobs remain
inspectable after restarts, and completed extraction can be resumed when its
checkpoint is compatible.

```bash
bash scripts/run_backend.sh
```

```bash
bash scripts/run_frontend.sh
```

Open `http://127.0.0.1:5173`, select a PDF, inspect **Run settings**, and click **Run pipeline**. The UI shows live activity, four assigned task models, stage decisions, selected passages, joint assessments, graph views, and audit outcomes. Model selectors are configured through environment variables; run limits are configurable in the UI.

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
2. Ask DeepSeek Flash for atomic claims from explicit target regions while preserving surrounding reading context. Whole-packet extraction remains selectable. Exact sentence IDs reconstruct source evidence, including multi-sentence references.
3. Check valid JSON, required fields, data types and literal source anchors. Relation names are open; the default numeric claim cap is zero. Finite generation budgets still apply.
4. No standalone semantic or keyword-sampled document scope model call is made. Deterministic unusable-text detection remains; scientific scope and contextual types require retrieved evidence assessment.
5. Normalize only high-confidence local vocabulary matches; preserve original labels and never invent ontology IDs.
6. Validate source fidelity with a separate stateless source-verifier call plus deterministic anchor and modality checks. A generic disease mention may use an exact local section heading as a bounded antecedent; distant context cannot prove the claim. Explicit hedges such as `may`, `can`, and `could` remain conditional.
7. Resolve all references attached to source evidence, using bibliography DOIs directly and conservative Crossref matching only when needed. Search Europe PMC and OpenAlex with literal and measurement queries. Rank metadata using BM25/BGE fusion and MedCPT; retain a small allowance for potentially accessible papers lacking indexed abstracts. Fetch up to 8 search papers by default (UI: 1..30), plus all usable attached source citations.
8. Prefer Europe PMC full-text XML, then an explicit OpenAlex open-access PDF or indexed abstract. Original file bytes stay transient. A bounded, expiring shared index reuses parsed text, sentence-indexed chunks and versioned embeddings across runs; selected proof also stays in each claim's audit.
9. Rank sentence-aligned chunks against the full claim/qualifiers and a neutral exposure/outcome query; retain 4 per paper by default. Trust graph mode assembles provisional paths, shares related claims' DeepSeek paper assessments, and uses MiMo Pro for cross-paper synthesis. Paper calls start as documents become ready. Every edge retains its own proof and decision; source citations remain distinct from independent findings. Pending retrieval and oversized inputs remain visible rather than silently losing papers.

Only eligible validated edges enter the final graph. Provisional pathways and
evidence balance remain separately inspectable; connecting A-to-B and B-to-C
does not establish a direct causal A-to-C finding. The previous validated-first
flow is selectable. See [evidence reuse and scheduling](docs/protocols/validation_speed.md).
10. Write a stratified evaluation sample with blank gold-label fields for independent review and measurable precision/recall.

Repeated claims from different source spans remain separate validation units. Only exact overlapping duplicate occurrences are collapsed. This prevents a good occurrence from hiding a weak or incorrectly typed occurrence.

## Data and code

See [the folder guide](backend/README.md#backend-architecture) for the complete layout and file-management rules.

| Location | Purpose |
| --- | --- |
| `pdf/` | Source PDFs; never removed by the pipeline |
| `backend/api/app.py` | FastAPI PDF, run, log, claim, graph, and upload endpoints |
| `backend/pipelines/document.py` | Run orchestration, checkpoints and artifacts |
| `backend/stages/` | Parsing, extraction, source checks, queries, retrieval, ranking, external judgement, graph and evaluation stages |
| `backend/domain/ontology.py` | Stable type/relation vocabulary and pathway-shape policy |
| `backend/stages/external_validation/joint.py` | One multi-paper Pro assessment and evidence-grounded repair eligibility |
| `backend/stages/source_validation/checker.py` | Exact-span source entailment, relation, direction, and modality checks |
| `backend/domain/vocabulary.py` | Conservative local ChEBI/ontology normalization |
| `backend/stages/evaluation/metrics.py` | Stratified evaluation sample and metric calculation |
| `backend/stages/graph_construction/graph.py` | Evidence-state graph snapshots and provisional paths |
| `backend/providers/` | LLM and optional NLI clients |
| `backend/domain/` | Shared ontology, vocabulary, source alignment and modality rules |
| `frontend/src/components/` | Claim, graph, workspace and model-diagnostic UI components |
| `frontend/src/styles/` | Shared base styles and light theme |
| `backend/config/paths.py` | Shared paths for PDFs, results, prompts and the frontend build |
| Stage-owned prompt resources | Extraction template in `claim_extraction/`; external reference in `external_validation/` |
| `tests/` | Backend, external-validation, project and versioned regression tests |
| `ontology/<PDF stem>/` | Per-PDF candidates, validation records, graph, logs, summary, and evaluation sample |

Whole external articles are never saved. The graph distinguishes independently corroborated, independently single-source, source-reference-only, contested, contradicted, and insufficient-evidence states. `external_evidence_not_found` means the bounded retrieval run found no acceptable evidence; it is not a false verdict.

## Evaluation

Each completed run creates `evaluation_sample.jsonl`. Fill its `gold` fields without changing the recorded prediction, then score it:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Generate a fresh stratified sample if required:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation sample "ontology/<PDF stem>" --limit 80 --output reports/evaluation_sample.jsonl
```

The standalone external validator remains available through `python -m backend.cli.external_validation`; see [backend/stages/external_validation/README.md](backend/stages/external_validation/README.md) and [docs/CONTEXT.md](docs/CONTEXT.md).

## Development checks

Install test dependencies with `./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`.
From the repository root, run `./.venv/Scripts/python.exe -m pytest`.
In `frontend/`, run `npm test` and `npm run build`.
See [docs](backend/README.md) for current protocols and [tests](tests/README.md) for test groups.
