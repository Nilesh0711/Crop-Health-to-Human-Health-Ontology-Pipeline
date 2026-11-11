# Crop Health Research Workbench

> **Current research mode, 11 November 2026:** Extraction contract v10 and profile v9 use 203 established ontology classes and 10 established RO/BFO properties. Project-added scientific definitions, synonyms and properties are not supplied for grounding. The active pipeline extracts then grounds; manual **Run grounded claims** also reuses saved extraction. Grounding groups up to three claims with the same evidence, using five workers. Extraction Thinking is Off; grounding Thinking is On. Suspected extraction omissions remain visible under review holds and cannot share concept identities or enter downstream assembly until reviewed. Scientific validation, pathway assembly and reasoning remain paused. Older descriptions below concern historical workflows. See the [current grounding protocol](backend/stages/ontology_grounding/README.md).

The active profile covers **potato, soil nutrients and crop nutrition** using the
203-class/10-property established-only module. Extraction receives every approved
class name and each property name with a short relationship hint; full ontology definitions and identifiers
are supplied during grounding. Extraction no longer requests certainty or
provisional subject/object classifications. The **Entity registry**
keeps valid endpoint groundings visible independently of complete-claim mapping;
uncertain alignments and individual observations remain separate. Start with the
[small grounding pilot](analysis/grounding_pilot/README.md) before expanding
the scope. Thinking defaults to Off for extraction and On for grounding; isolated comparisons
are available without changing saved production results.


The [v50 model update](docs/protocols/muse_external_model.md) replaces MiMo Pro with Muse Spark 1.3 Contributor for synthesis and quotation audits. The discounted Go route requires a workspace privacy opt-in. With Muse, an unchecked external Thinking switch requests minimal reasoning; complete disablement is unsupported.

Current application: **v53** (`ontology-grounding-v53`). Start with the
[documentation index](docs/README.md).

The [v49 update](docs/protocols/proof_audit_atomic_reextraction_and_layout.md) adds independent external-quotation audits before supported-graph admission, bounded arithmetic assistance, deliberate atomic re-extraction and column-aware parsing for fresh runs. Saved extraction retains its parsing policy.

The historical v50 pipeline extracted claims, checked PDF fidelity and project scope,
assembles a globally aligned candidate graph, then externally assesses **two
neighboring claims and their local connection**. MiMo Flash plans searches,
DeepSeek Flash judges papers and Muse Spark 1.3 Contributor synthesizes local findings. Longer
chains are assembled from local connections; their whole-chain correctness is
explicitly **not assessed**. No numerical trust factor or NLI runs in this flow.
See the [active protocol](docs/protocols/neighboring_claim_validation.md).

The [qualified evidence contract](docs/protocols/qualified_neighbor_assessment.md)
distinguishes exact proposition matches, partial wording and contextual evidence.
Corrections are explicit review proposals; originals are preserved. Scientific
support and execution completeness appear separately. Older local judgements need
refresh under this contract. Saved extraction remains reusable; old-policy source
checks refresh without a separate automatic reassessment loop.

The [v48 identity and atomicity update](docs/protocols/identity_atomicity_and_qualified_support.md)
checks declared registry joins against actual assignments, distinguishes
substances/processes/material states, improves scientific-equivalence instructions
and shows qualified component support separately. Exclusion contrasts remain
auditable without becoming positive supported paths. No bridge generation or
whole-chain mechanism expansion is added.

Compatible saved extraction/source decisions remain resumable. Pair and claim-paper
findings have exact scientific fingerprints; unchanged paper material, embeddings
and cross-encoder scores can be reused. Redo creates fresh judgement checkpoints.
Retired full-route, trust-group and per-claim executors are isolated under
[archive/legacy_pipeline](archive/legacy_pipeline/README.md).

The source model records PDF fidelity and project relevance separately in the
same call. Only source-entailed, explicitly in-scope claims enter provisional
assembly; economic/financial and other excluded assertions remain visible for
audit. Resume saved extraction to refresh older source checks under the new
scope policy. No document-wide keyword screen or extra scope model call is used.

Automatic source reassessment is disabled. Researchers can manually **Accept** or
**Reject** PDF fidelity and project scope separately, keeping the original model
reading and a review history. These controls make no LLM calls. Resume saved
pathways after reviewing claims to rebuild affected routes.

Use **Redo external validation** beside saved-run continuation to reassess all
selected neighboring pairs with fresh external checkpoints. Saved extraction and
compatible source/manual decisions are retained. Earlier results remain archived;
continuing an interrupted redo resumes that same assessment instead of restarting it.

To share saved results without running models, use the existing UI's read-only
viewer and the prepared Render Free configuration. See the
[deployment guide](docs/deployment/render_viewer.md) for snapshot export, local
preview, deployment and updates.

A local-first research application for building soil-to-human-health knowledge
pathways from scientific PDFs. Exact source evidence stays attached to claims,
while external literature assesses neighboring claims and their local conditions.
Connecting two edges proposes a pathway; it does not establish a direct causal
shortcut or replace experimental evidence.

## Start from Git Bash

Requirements: Python 3.11+, Node.js 20+, an OpenCode API key with access to the selected Go models, and internet access for Europe PMC, Crossref, OpenAlex, and first-use Hugging Face model downloads. The default backend calls inference directly; OpenCode CLI and Ollama are not required.

Each task uses its assigned primary model. Model errors appear in the frontend
activity log. One bounded same-model recovery may propose formatting edits or
regenerate missing response content from original evidence; no local JSON
correction or fallback model changes the judgement. Saved compatible checkpoints
can resume after failure. See [provider behavior](backend/providers/README.md).

Ontology grounding uses JSON output and supplies all approved classes without
shortlisting. TSV is retained as an explicit comparison format. See the
[grounding output contract and paired format benchmark](backend/stages/ontology_grounding/README.md).
Extraction still uses schema-based JSON output. Neither format establishes scientific merge
correctness by itself.

New extraction uses the [five-required-field contract (v8)](backend/stages/claim_extraction/README.md):
subject, relation, object, qualifiers and evidence sentence IDs; type hints are
optional. Short optional subject_context/object_context strings keep endpoint
restrictions separate. Grounding reads these with exact source evidence; no
generated claim summary or extra LLM stage is introduced.

Run setup once:

```bash
bash scripts/setup.sh
```

Configure `OPENCODE_API_KEY` in the backend environment, or use
`python -m backend.cli.configure_api_key` on Windows to store it in Credential
Manager. See [API formatting, bounded repair and resume](backend/providers/README.md).

Then keep two Git Bash terminals open:

Backend automatic reload is enabled by default for Python and prompt files under
`backend/`. Editing those files restarts the backend and interrupts active
requests. Generated results, PDFs and archived scripts do not trigger reload.
Use `BACKEND_RELOAD=0 bash scripts/run_backend.sh` when you want to disable it.
Saved jobs remain inspectable after restarts, and compatible checkpoints can resume.

```bash
bash scripts/run_backend.sh
```

```bash
bash scripts/run_frontend.sh
```

Open `http://127.0.0.1:5173`, select a PDF, inspect **Run settings**, and click **Run pipeline**. The UI shows live activity, assigned models and Thinking switches, source decisions, paper evidence, pathway assessments, graph views and audit outcomes. Model selectors are configured through environment variables; run limits are configurable in the UI.

The first validation run may take longer while these local specialist models download and initialize:

- Dense retriever: `BAAI/bge-base-en-v1.5`
- Biomedical cross-encoder: `ncbi/MedCPT-Cross-Encoder`

Override model names and devices through the [configuration guide](backend/config/README.md). Bare Settings defaults to CPU; the launcher selects CUDA when available unless a device was explicitly configured. CPU execution requires sufficient system RAM. Hosted generative models do not use the local GPU.

For local GPU setup on Windows, run the installer below, then restart the backend:

```bash
bash scripts/enable_gpu_models.sh
```

For one-server use, run `npm.cmd run build` inside `frontend/`, then start FastAPI; it serves the built UI at `http://127.0.0.1:8000`.

## Pipeline

1. Parse PDF chapters, paragraphs and bibliography links. Deterministic quality
   checks report unreadable or unusable text; they do not judge scientific scope.
2. DeepSeek Flash extracts atomic claims from explicit target regions with
   surrounding reading context. Whole-packet extraction remains selectable.
   Sentence IDs reconstruct exact evidence across one or more source sentences.
   The default numeric claim cap is zero; finite generation budgets still apply.
3. Validate response structure and literal source anchors. DeepSeek Flash checks
   PDF fidelity and project scope separately in the same call, with three claims
   per call and up to two independent batches in flight by default. Every claim
   receives its own verdict using only its attached evidence. Excluded or
   unresolved claims and their evidence remain visible for audit. Human Accept /
   Reject decisions preserve the model reading and review history.
4. MiMo Flash aligns source-grounded, in-scope entities globally and assembles
   provisional directed pathways. Original relations, conditions and source
   context remain attached; assembly does not prove the route.
5. MiMo Flash plans literature queries for two neighboring claims and their local
   connection conditions. Europe PMC/OpenAlex searches and resolved source
   citations supply candidate papers. BM25/BGE and MedCPT rank candidates and
   passages. Source-cited papers remain distinct from independent discoveries.
6. Fetch usable full text or abstracts and retain traceable sentence-indexed
   chunks. The default search budget is eight papers, configurable from 1 to 30,
   plus all usable resolved citations. A bounded shared index reuses parsed text
   and embeddings across runs; current targets still determine passage ranking.
7. DeepSeek Flash assesses the selected evidence paper by paper. Muse Spark 1.3 Contributor then
   synthesizes findings for the neighboring pair, distinguishing claim evidence
   and compatible local biological conditions. Exact paper/chunk/sentence checks
   prevent misplaced proof. Whole-chain causation remains unassessed.
8. Save paper assessments, pathway outcomes, evidence, graphs, logs and evaluation
   samples. Pending retrieval, insufficient evidence and technical failure stay
   distinguishable from contradiction. Compatible completed work can resume.

The current app and CLI validate neighboring claim pairs. Retired per-claim,
whole-route and trust-graph executors are isolated under
[`archive/legacy_pipeline`](archive/legacy_pipeline/README.md) for
research comparisons. Historical results remain readable. Repeated claims from
different source spans remain separate occurrences; only genuine overlapping
duplicates are collapsed.

## Data and code

See [the folder guide](backend/README.md#ownership) for package ownership and file-management rules.

| Location | Purpose |
| --- | --- |
| `pdf/` | Source PDFs; never removed by the pipeline |
| `backend/api/` | FastAPI composition, request contracts and document/run/results routes |
| `backend/pipelines/document.py` | Run orchestration, checkpoints and artifacts |
| `backend/stages/` | Parsing, extraction, source checks, queries, retrieval, ranking, external judgement, graph and evaluation stages |
| `backend/domain/ontology.py` | Stable type/relation vocabulary and pathway-shape policy |
| `backend/stages/neighbor_validation/` | Local-pair paper assessment, synthesis, traceability and saved results |
| `backend/stages/external_validation/` | Shared exact-text utilities and historical proof readers |
| `backend/stages/source_validation/checker.py` | Model assessment of PDF fidelity/scope and deterministic verification of returned source anchors |
| `backend/domain/vocabulary.py` | Conservative local ChEBI/ontology normalization |
| `backend/stages/evaluation/metrics.py` | Stratified evaluation sample and metric calculation |
| `backend/stages/graph_construction/graph.py` | Source/audit graph snapshots, GraphML and historical corroborated health paths |
| `backend/providers/` | Hosted LLM transport, recovery and local-model ownership |
| `backend/domain/` | Shared ontology, vocabulary, source alignment and modality rules |
| `frontend/src/components/` | Claim, graph, workspace and model-diagnostic UI components |
| `frontend/src/hooks/` | Navigation, concurrent finding loading and run/recovery lifecycle |
| `frontend/src/styles/` | Consolidated light-theme styles |
| `backend/config/paths.py` | Shared paths for PDFs, results, prompts and the frontend build |
| Stage-owned prompt resources | Extraction template in `claim_extraction/`; external reference in `external_validation/` |
| `tests/` | Backend, pathway-validation, architecture and historical regression tests |
| `archive/legacy_pipeline/research_historical/` | Retired executable workflows and explicit research configuration |
| `ontology/<PDF stem>/` | Per-PDF candidates, validation records, graph, logs, summary, and evaluation sample |

Downloaded PDF/XML bytes remain transient. Parsed text can persist in the bounded evidence index; selected proof persists in run artifacts. Pathway outcomes distinguish supported, partial, contradicted and insufficient evidence. Historical claim-state labels remain visible for old runs. A bounded search with no usable evidence does not establish that a claim is false.

## Evaluation

Runs retain `evaluation_sample.jsonl` for claim/source annotation and
`neighbor_evaluation_sample.jsonl` for current local-pair review. Fill `gold`
fields independently of model predictions. The existing scoring command handles
the claim-level schema; it does not calculate whole-pathway precision/recall:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Generate a fresh stratified sample if required:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation sample "ontology/<PDF stem>" --limit 80 --output reports/evaluation_sample.jsonl
```

The CLI `python -m backend.cli.external_validation validate --pdf <path>` invokes the same neighboring-pair flow. Its offline tuple importer and health check remain available; see [CLI details](backend/stages/external_validation/README.md).

## Development checks

Install test dependencies with `./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`.
From the repository root, run `./.venv/Scripts/python.exe -m pytest`.
Run `./.venv/Scripts/python.exe -m ruff check backend` to check undefined names.
In `frontend/`, run `npm test` and `npm run build`.
See [documentation](docs/README.md) for current protocols and [tests](tests/README.md) for test groups.
See [recorded verification](tests/README.md#current-verification) for the latest
suite results. Documentation and offline contract checks do not establish a
scientific accuracy improvement; independent gold labels are required.
