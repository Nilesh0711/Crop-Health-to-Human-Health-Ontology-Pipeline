# Project context: soil-to-human-health knowledge graph

Last updated: 2026-10-10

Current pipeline contract: `external-validation-v14`

## Objective and boundary

Build a provenance-rich research knowledge graph along this bounded pathway:

`Agriculture / Climate -> Soil -> Crop / Disease / Trait -> Nutrient / Contaminant -> Human Exposure -> Human Biological Process / Health Outcome`

The goal is high-precision, evidence-backed edges, not maximum triple count. Upstream-only soil/crop papers are useful for pathway segments, but economics, infrastructure, trade, generic food security, and yield-only claims are excluded. A connected path is a research hypothesis; `A -> B` plus `B -> C` never creates a new direct `A -> C` fact.

Path-level composition validation is deliberately deferred. Current work is limited to correct extraction, semantic/schema fit, source faithfulness, and external literature support for individual edges.

## Application and data policy

- Backend: FastAPI in `backend/`.
- Frontend: React/Vite/Cytoscape in `frontend/`.
- Source PDFs: `pdf/`.
- Per-PDF outputs: `ontology/<PDF stem>/`.
- Archived old run data: `archive/`.
- Whole external articles are never saved. Europe PMC XML and OpenAlex public PDFs are processed transiently in memory; only selected evidence chunks plus bibliographic, page, access, and decision metadata persist.
- Rerunning a PDF rewrites that PDF's current derived output files. Source PDFs are not removed.

## Controlled vocabulary: keep stable for now

Do not add more entity types merely to improve acceptance rates. The current set is sufficient for this phase:

`AgriculturalPractice`, `ClimateFactor`, `SoilMaterial`, `SoilCondition`, `SoilChemicalProperty`, `SoilBiota`, `Crop`, `FoodProduct`, `PlantDisease`, `PlantPest`, `PlantProcess`, `PlantTrait`, `Nutrient`, `Contaminant`, `HumanExposure`, `HumanBiologicalProcess`, `HumanHealthOutcome`.

Automatic extraction relations are:

`affects`, `reduces`, `increases`, `associated_with`, `has_nutrient`, `has_contaminant`, `contains`, `has_trait`, `higher_than`, `lower_than`.

`causes`, `leads_to`, and `deficiency_causes` remain deferred to a stricter causal-evidence workflow. Association must never be upgraded to a directional or causal relation. Model confidence is metadata and is never an acceptance rule.

## Current correctness-first pipeline

1. Extract PDF text into a continuous section-aware sentence stream. Remove obvious publisher/front-matter noise and form overlapping packets of about 2,000 words rather than many shallow subsection prompts.
2. Classify document scope and text quality. Scope is advisory; unusable text blocks the run and requires OCR or a cleaner PDF.
3. Give Qwen the full packet plus a ranked index of likely mechanistic sentences. Emit at most four claims per packet by default; zero is valid.
4. Require each claim to select one to three consecutive source sentences and exact copied subject, object, and relation-trigger mentions. Reject malformed, generic, unanchored, purpose-only, and invalid comparison claims before further model calls.
5. Preserve atomicity: wording such as `X forms M, which increases Y` may support `X -> M` and `M -> Y`, but never a collapsed `X -> Y` edge.
6. Clean only redundant qualifiers. In particular, a practice repeated in `crop_or_species` is removed and audited; genuine species, population, comparator, metric, dose, and condition restrictions remain.
7. Run structural schema validation, then a fresh Qwen semantic decision for project scope, entity types, relation strength, direction, and atomicity. Only unambiguous type normalization is allowed; the system never silently rewrites a relation or direction.
8. Apply conservative vocabulary normalization. Exact reviewed mappings may receive ontology identifiers; fuzzy identifier invention is forbidden.
9. Run source entailment with a separate stateless Qwen call plus deterministic span, surface, relation, direction, and modality checks. Full-sentence modal detection preserves `may`, `can`, and `could` as conditional. Harmless domain modifiers such as `aeration` / `soil aeration` are allowed, but different crops or materials are not.
10. Resolve both numeric citations and author-year citations from the exact source evidence span. Resolve bibliography text through Crossref, then look up that exact work in Europe PMC or OpenAlex. A citation is only a retrieval lead and never support by itself; its selected full-text passage or abstract must pass entailment.
11. Search broad literature in both Europe PMC and OpenAlex. OpenAlex is necessary because agronomy coverage in Europe PMC alone is incomplete. The originating PDF is excluded from external corroboration.
12. Rank article metadata with BM25 and BGE-base using reciprocal-rank fusion, then rerank leading candidates with MedCPT. Prefer Europe PMC XML, then a transient OpenAlex public PDF, then an indexed abstract.
13. Ask Qwen to judge every external document independently. It must select one or two consecutive sentences and match subject, object, relation, direction, certainty, and relevant qualifiers.
14. Deterministic evidence checks require exact quoted source mentions and relation cues, but accept a deliberately small lexical-normalization set such as microbial/microorganism, community/composition, and suppressiveness/suppression. They still reject material substitutions such as organic fertilizer versus suspension fertilizer.
15. DeBERTa NLI is only a conservative contradiction veto. It cannot create support or replace Qwen as the evidence judge.
16. Count source-reference support separately from independent broad-search support. DOI-identical records from Europe PMC and OpenAlex are deduplicated and count as one study group.
17. Build three graph views: external main graph, source-supported graph, and audit graph.

## Why the previous run had almost no external support

The `Exploring the link between soil health and crop productivity` v13 run had 18 extracted claims, 11 semantic acceptances, 8 source-entailments, and zero main-graph edges. The dominant problem was not that every claim was false:

- The paper used author-year citations, while the old parser only linked numbered citations, so every claim had zero cited-paper candidates.
- Europe PMC alone has incomplete agronomy coverage.
- Qwen sometimes put ontology labels such as `SoilBiota` into the search query.
- Practices such as crop rotation were incorrectly duplicated into `crop_or_species`, creating impossible external constraints.
- The final guard required near-verbatim entity phrases and downgraded valid variants such as `soil microbial composition` / `soil microbial communities` and `disease suppressiveness` / `disease suppression`.
- Some genuinely weak claims still deserve review, especially collapsed multi-hop statements, different interventions/materials, reversed direction, and stronger certainty than the source.

Version v14 fixes those systematic retrieval/alignment errors without weakening relation, direction, qualifier, or provenance requirements. Existing v13 files do not change automatically; rerun the PDF to obtain v14 outputs.

## Evidence states and graph admission

- `source_supported`: faithful to the local PDF, but not yet externally supported.
- `source_reference_supported`: supported by a paper cited in the local source; useful, but not independent corroboration.
- `single_source`: at least one independently retrieved study supports the edge; included in the main external graph.
- `corroborated`: at least two independent publication/study groups support the edge.
- `not_found` / `external_evidence_not_found`: bounded retrieval found no acceptable evidence; this is not a false verdict.
- `contradicted` or `contested`: explicit conflicting external evidence exists.
- Semantic/source rejection and review states remain in the audit graph and never enter the main graph.

## Models and runtime

- Generative extractor/judge: local Ollama `qwen3:8b`.
- Dense bi-encoder: `BAAI/bge-base-en-v1.5` on CPU by default.
- Cross-encoder reranker: `ncbi/MedCPT-Cross-Encoder` on CPU by default.
- NLI contradiction veto: `cross-encoder/nli-deberta-v3-base` on CPU by default.
- Qwen calls are stateless; there is no rolling KV cache across extraction packets.
- Environment overrides are in `scripts/run_backend.sh` and `external_validator/config.py`.

Start from Git Bash with three terminals:

```bash
bash scripts/run_ollama.sh
bash scripts/run_backend.sh
bash scripts/run_frontend.sh
```

Backend code changes require a backend restart unless it was started with reload. Frontend changes require a Vite restart only when its running process does not hot-reload them.

## Important output files

- `candidates.jsonl`: canonical extracted candidates and source provenance.
- `extraction_rejections.jsonl`: extraction-stage discards with reasons.
- `schema_valid_candidates.jsonl`: structurally valid candidate IDs.
- `semantic_validation.jsonl`: scope/type/relation/direction/atomicity decisions.
- `source_validation.jsonl`: local source-entailment decisions.
- `validation_runs.jsonl`: retrieval packets and per-external-document decisions.
- `graph.json`: independently externally supported edges only.
- `source_graph.json`: source-entailed edges.
- `audit_graph.json`: rejected, review, contradicted, and externally unsupported edges.
- `summary.json`: stage counts, model configuration, and graph counts.
- `run_log.jsonl`: frontend activity log.
- `evaluation_sample.jsonl`: immutable predictions with blank gold fields for independent evaluation.

## Measurement and next work

Acceptance rate is an operational diagnostic, not accuracy. A quality percentage requires labels independent of the extracting Qwen model. The generated stratified evaluation sample minimizes review work but intentionally leaves gold fields blank.

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Immediate next step: restart the backend and rerun one representative PDF with v14, then inspect author-year `source_references`, Europe PMC/OpenAlex result counts, selected chunks, and the exact reasons for remaining review decisions. Do not start path-level reasoning until edge-level precision and external-support coverage are measured.

## Key implementation files

- `backend/pipeline.py`: PDF packetization, extraction contract, citation/qualifier handling, orchestration.
- `backend/ontology_schema.py`: stable entity/relation vocabulary and legal pathway shape.
- `backend/semantic_validation.py`: Qwen semantic gate.
- `backend/source_validation.py`: source-entailment and modality safeguards.
- `backend/vocabulary.py`: conservative entity normalization.
- `backend/graph.py`: evidence-state graph construction.
- `external_validator/workflow.py`: citation and broad retrieval, ranking, judgement, evidence accounting.
- `external_validator/retrieval.py`: Europe PMC, Crossref, and OpenAlex clients plus transient passage extraction.
- `external_validator/decision.py`: external Qwen judgement and deterministic evidence invariants.
- `external_validator/rerank.py`: BM25/BGE RRF and MedCPT reranking.
- `external_validator/nli.py`: contradiction-only NLI check.

Reference slides: `ppt/Reasoning with Food Knowledge Graphs.pptx` and `ppt/Rag approach for knowledge graph validation.pptx`.
