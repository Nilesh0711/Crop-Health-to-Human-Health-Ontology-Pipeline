# Project context: soil-to-human-health knowledge graph

## Research goal

Build a provenance-rich graph for the path **Agriculture/Climate → Soil → Crop/Disease → Nutrient/Contaminant → Human Exposure → Health Outcome**. We prefer a small number of faithful, independently supported relations to many plausible triples. The graph is for research exploration; a connected multi-hop path is not automatically a causal conclusion.

## Current application (2026-10-09)

The FastAPI backend (`backend/`) and React/Cytoscape frontend (`frontend/`) let a user run one PDF at a time, view logs, inspect each claim and its evidence, and switch between a supported graph and an audit view. Source PDFs are in `pdf/`; outputs are in `ontology/<PDF stem>/`. `README.md` has startup commands.

`backend/pipeline.py` extracts text with `pypdf`, forms section-aware overlapping packets targeting ~2,000 words, and sends each packet to local Ollama `qwen3:8b` with an 8,192-token context and up to 2,600 extraction-output tokens. A JSON schema limits each packet to at most ten high-value claims and constrains entity/relation labels. Qwen returns only the claim and 1–3 evidence sentence numbers; the pipeline derives the exact PDF evidence span itself, so the model does not repeat quote text. There is no minimum claim quota or confidence-based acceptance threshold.

`backend/ontology_schema.py` defines the bounded vocabulary, including `PlantTrait`, `HumanBiologicalProcess`, and `has_contaminant`. It checks only that extraction uses those stable labels. `backend/semantic_validation.py` separates entity classification from relation/scope classification so the 8B model handles fewer decisions per call. The gate explicitly accepts a useful intermediate pathway edge even when that single claim does not reach human health. Suggested corrections are stored for review and never silently rewrite a claim.

`backend/source_validation.py` then makes another stateless Qwen call asking whether the selected local evidence span explicitly supports the exact subject, object, relation, direction, and certainty. Deterministic checks require exact provenance and explicit entity/relation proof. Ambiguous, unsupported, or failed checks do **not** proceed to external retrieval. `candidates.jsonl` is the one canonical full candidate store. `schema_valid_candidates.jsonl`, `semantic_valid_candidates.jsonl`, and `source_entailed_candidates.jsonl` are intentionally minimal candidate-ID indexes; stage decision files contain only the decision, rationale, necessary proof, and optional correction/rejection details.

Only source-entailed claims go to `external_validator/`: the BGE model is warmed up once, Europe PMC receives a concept-only query that excludes the originating title, BGE ranks article titles/abstracts before XML download and then ranks passages, and Qwen judges each selected external article. A positive judgement must cite retained text and pass entity/direction/certainty guardrails. No automatic validation retries. Only selected external passages and metadata persist; full papers are not saved. `backend/graph.py` puts externally supported edges in the main graph and all others in the audit view. Paths still use only corroborated edges, are per-PDF, and have **no path-level validation yet**; path correctness is deliberately deferred.

## What the saved data showed

The pre-change baseline at `analysis/extraction_baseline_2026-10-08.jsonl` snapshots **105 claims across five PDFs**; 39 passed the prior scope/schema rules, 68 pass the revised structural rules. That increase is not evidence of better scientific accuracy. Examples motivating the new checks: “False Smut Disease affects Rice” cited a sentence about FSD spatial patterns, not the relation; “Rhizosphere microorganisms affect nutrient transport” used “these aggregates” as its unsupported subject; external quotes about a different fertilizer were counted for “organic fertilizer.” Conversely, a directly stated fertilizer→crop-cadmium relation had been blocked by the old overly rigid stage schema.

Use `scripts/extraction_audit.py compare --baseline analysis/extraction_baseline_2026-10-08.jsonl` after fresh runs to compare counts and exact triple overlap. Optional Qwen review in the audit script is **provisional**, not independent expert ground truth; it cannot establish measured precision or recall. No trained relevance/type classifier is deployed yet because there is no independent labelled set. Current low-cost gate is deterministic schema/scope checking, followed by Qwen for source ambiguity.

## Remaining limits

- Local entity-phrase matching is intentionally conservative; aliases, abbreviations, and paraphrases may be sent to review. Add a small curated alias map or expert-reviewed labels before optimizing recall.
- An external article may support the same wording in a different species, dose, geography, or population. Qwen is asked to check these qualifiers, but this remains a model judgement requiring spot checks.
- PDF text extraction is imperfect (tables, columns, bullets, OCR). Preserve source spans and abstain on unclear evidence.
- Multi-paper consensus, merged-graph canonicalization, and path-level validation are later milestones. Never infer a direct A→C edge from A→B and B→C alone.

Reference slides: `ppt/Reasoning with Food Knowledge Graphs.pptx` and `ppt/Rag approach for knowledge graph validation.pptx`. `deliverables/` are prior research artifacts, not fresh app output.
