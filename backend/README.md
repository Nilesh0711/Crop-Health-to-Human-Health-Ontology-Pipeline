# Backend architecture (v50)

The [v50 model update](../docs/protocols/muse_external_model.md) replaces MiMo Pro with Muse Spark 1.3 Contributor for synthesis and quotation audits. The discounted Go route requires a workspace privacy opt-in. With Muse, an unchecked external Thinking switch requests minimal reasoning; complete disablement is unsupported.

The current [evidence contract](../docs/protocols/qualified_neighbor_assessment.md)
qualifies proposition matches and correction proposals within the existing local
pair flow. Extraction checkpoints remain compatible; older source policies and
external assessments refresh without overwriting their original readings.
See [v48 identity and atomicity](../docs/protocols/identity_atomicity_and_qualified_support.md)
for declared registry joins, assembly version 2 and clearer qualified support.

The [v49 update](../docs/protocols/proof_audit_atomic_reextraction_and_layout.md) adds independent external-quotation audits before supported-graph admission, bounded arithmetic assistance, deliberate atomic re-extraction and column-aware parsing for fresh runs. Saved extraction retains its parsing policy.

The active scientific flow assesses neighboring claims, not complete routes.
Read the [protocol](../docs/protocols/neighboring_claim_validation.md) and
[configuration](config/README.md). The stable ASGI entry point remains
`backend.app:app`; original PDFs stay in `pdf/`, generated findings in `ontology/`.

## Ownership

| Package | Responsibility |
| --- | --- |
| `api/` | FastAPI catalogue/results, run/resume/redo/stop, source review and repair |
| `pipelines/document.py` | Parsing, extraction, source checks, assembly and local assessment |
| `pipelines/neighbor_validation.py` | Bounded concurrent local-pair assessment and checkpoints |
| `pipelines/checkpoints.py` | Extraction compatibility and saved-work recovery |
| `pipelines/claim_repair.py` | Targeted claim correction and affected-result invalidation |
| `pipelines/source_review.py` | Manual source/scope decisions without a model call |
| `stages/neighbor_validation/` | Adjacency targets, retrieval, findings cache, judgements and publication |
| `stages/query_planning/neighbors.py` | MiMo Flash local-link/junction queries |
| `stages/external_validation/` | Shared exact sentence/proof helpers and historical readers |
| Other `stages/` | PDF parsing, direct extraction, source validation, global identities, scholarly retrieval, ranking, graphs and evaluation |
| `domain/` | Scientific vocabulary/context and saved-record signatures |
| `providers/` | Direct OpenCode inference, bounded recovery, credentials and local ranking runtimes |
| `shared/` | Atomic storage, cache, cancellation, batching and activity formatting |
| `viewer/` | Isolated read-only snapshot export/service; no inference dependency in deployed startup |
| `cli/` | Research commands and credential setup |

Stages do not import API, pipeline entry points or archived executors. Package
imports do not load neural models. Dependency and archive-boundary tests enforce
these constraints.

## Scientific flow

1. Parse exact pages/sentences, chapters and bibliography links; assess text quality.
2. DeepSeek Flash extracts directly using target/context by default. Preserve
   claim_text, qualifiers and sentence-level evidence; no separate discovery pass.
3. DeepSeek Flash checks PDF fidelity and project scope separately. Manual
   Accept/Reject controls and source-defined abbreviation corrections remain.
   Three claims share one request by default, with two independent source
   batches concurrently; every claim retains its own evidence and verdict.
4. MiMo Flash aligns global entity occurrences using hybrid identity candidates
   and original context. Similarity never merges entities by itself.
5. Index incoming/outgoing original edges. Select two-edge pairs involving the
   document, including branches, without enumerating all longer validation routes.
6. MiMo Flash plans local searches; shared retrieval/ranking returns exact paper
   passages. DeepSeek Flash judges papers; Muse Spark 1.3 Contributor synthesizes local compatibility.
7. Publish current local support, source/audit graphs, proof, logs and evaluation
   samples. Longer assembled chains retain `whole_chain_assessment: not_assessed`.

NLI is removed from active code, settings and startup. No trust score grants
support. Selected primary models and Thinking Off defaults remain unchanged.
Source batches and local pairs have bounded concurrency; paper requests share a
global limit. Cross-encoder pairs are batched and cached by exact text/query and
identifiable weights. Per-device locks protect local-model inference.

## Recovery, persistence and historical views

Existing compatible v36 extraction/source/manual decisions can be resumed. A new
local policy/namespace prevents old full-route outcomes becoming local verdicts.
Resume preserves compatible completed work; Redo uses a fresh judgement namespace.
Source/condition/evidence/identity changes invalidate affected results. Stop
preserves completed checkpoints; missing jobs remain explicit in the frontend.

Atomic writes serialize their temporary file per normalized path and retain the
existing bounded Windows replacement retry. Query and claim-paper reuse never
bypasses exact paper/chunk/sentence grounding or scientific applicability checks.

Archived executors and dedicated v45 tests/protocols live in
[archive/legacy_pipeline](../archive/legacy_pipeline/README.md). Active readers
retain historical outcomes, old job settings and diagnostic fields without
executing numerical trust, whole-route or NLI logic.

Use `bash scripts/run_backend.sh` and the existing frontend launcher. A built UI
is served by FastAPI on port 8000. The separate published viewer supports free
Render deployment; see [deployment](../docs/deployment/render_viewer.md).
