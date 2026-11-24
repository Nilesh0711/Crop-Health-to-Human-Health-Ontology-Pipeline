# Crop Health Research Workbench

A local-first workbench for building soil-to-human-health knowledge from
scientific PDFs. The active profile covers **potato, soil nutrients and crop nutrition**.

> **Current production mode.** Run pipeline uses **bounded extraction Contract v25**
> with the nine established RO/BFO predicates in `closed-ro-bfo-9-v1`. Production
> stops after extraction. Use **Ground saved claims** in **Runs**
> for manual entity grounding. Merging, reasoning, pathway assembly and scientific validation are paused.

**Runs** shows saved claims with their selected relation, qualifiers, exact source
evidence and review warnings. An unheld claim is ready for source inspection.
**Appendix** opens on the active v25 prompt and output. The **Ontology** tab shows
the nine extraction properties first and offers the 101-property reference
catalogue for browsing.

Current application: **v53** (`ontology-grounding-v53`). See the
[documentation index](docs/README.md) and the [read-only viewer guide](docs/deployment/render_viewer.md).

## Start from Git Bash

Requirements include Python 3.11+, Node.js 20+, and an OpenCode API key with access
to the configured DeepSeek Flash model. The backend calls hosted inference directly.

Each task uses its assigned primary model. Model errors appear in the frontend
activity log. One bounded same-model recovery may propose formatting edits or
regenerate missing response content from original evidence; no local JSON
correction or fallback model changes the judgement. Saved compatible checkpoints
can resume after failure. See [provider behavior](backend/providers/README.md).

New extraction uses the [v25 JSON contract](backend/stages/claim_extraction/README.md).
Each assertion uses one approved property. The verbatim relation cue, evidence
sentence IDs and qualifiers stay attached to the source finding.

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

Open `http://127.0.0.1:5173`, select a PDF in **Runs**, and click **Run pipeline**.
The extraction settings include page selection, target size, generation budget
and Thinking. Defaults are 200-word targets, two workers, a 50,000-token generation
allowance and Thinking On. Open **Claims** and expand a finding to see its
qualifiers, source passage and any review warnings.

For one-server use, run `npm.cmd run build` inside `frontend/`, then start FastAPI; it serves the built UI at `http://127.0.0.1:8000`.

## Pipeline

1. Parse PDF chapters, paragraphs and bibliography links. Deterministic quality
   checks report unreadable or unusable text; they do not judge scientific scope.
2. DeepSeek Flash extracts atomic claims from explicit target regions with
   surrounding reading context. Whole-packet extraction remains selectable.
   Sentence IDs reconstruct exact evidence across one or more source sentences.
   The default numeric claim cap is zero; finite generation budgets still apply.
3. Validate the predicate alias, response structure and source anchors. Save
   accepted proposals, review holds and rejected proposals with their evidence.
   Repeated findings from distinct source spans remain separate occurrences.

**Ground saved claims** grounds the endpoints of every unheld finding for the paper.
It keeps the extracted assertion predicates, qualifiers and evidence fixed. Inspect the result in
**Mapping Review**. See the
[grounding reference](backend/stages/ontology_grounding/README.md) for hybrid
retrieval and earlier grounding policies. The
[neighboring-claim protocol](docs/protocols/neighboring_claim_validation.md)
documents the historical pathway-validation workflow.

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

Historical evaluation samples can still be scored with independently reviewed
`gold` fields. The existing scoring command uses the claim-level schema:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Generate a fresh stratified sample if required:

```bash
./.venv/Scripts/python.exe -m backend.cli.evaluation sample "ontology/<PDF stem>" --limit 80 --output reports/evaluation_sample.jsonl
```

For current extraction, review source fidelity, atomicity, predicate fit and
retained findings separately.

## Development checks

Install test dependencies with `./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`.
From the repository root, run `./.venv/Scripts/python.exe -m pytest`.
Run `./.venv/Scripts/python.exe -m ruff check backend` to check undefined names.
In `frontend/`, run `npm test` and `npm run build`.
See [documentation](docs/README.md) for current protocols and [tests](tests/README.md) for test groups.
See [recorded verification](tests/README.md#current-verification) for the latest
suite results. Documentation and offline contract checks do not establish a
scientific accuracy improvement; independent gold labels are required.
