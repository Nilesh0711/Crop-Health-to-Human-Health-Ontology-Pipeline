# Project context: soil-to-human-health knowledge graph

Last updated: 2026-10-14

Current pipeline contract: `external-validation-v23`

## Generative model roles (v23)

`opencode/longcat-2.5-preview-free` is the dedicated claim-extraction model. It receives
section-aware packets of roughly 4,000 words (about 5,600 source tokens), with
a 500-word overlap; the larger packet preserves mechanism and qualifier context
without weakening the requirement that each accepted claim cite only one to
three consecutive source sentences.

`opencode/step-5-preview-free` is the independent evidence judge for document
scope, semantic typing/relevance, source entailment, retrieval-query planning,
and per-document external-evidence decisions. It is intentionally not
the extractor, so a claim is reviewed by a different generative model.

`opencode/cat-jam-free` is deliberately not in the production path: its model
identity is undisclosed and its free-tier prompts are retained for 30 days,
whereas OpenCode documents zero retention for LongCat and Step 5. Local Qwen
is no longer used by the FastAPI pipeline or the external-validator CLI. The
local BGE, MedCPT, and DeBERTa NLI models remain because they are specialised
retrieval/contradiction components, not a general-purpose LLM judge. OpenCode
credentials remain owned by the installed OpenCode client. The pipeline invokes
its CLI with a temporary attached input file, then deletes it; no credentials or
whole external articles are stored.

## Latest validation change (v23)

The October 14 v22 rerun of `Crop health and its global impacts on the
components of food security` produced 7 claims, 5 semantic acceptances, 2
source entailments, and zero externally supported edges. The same source
phrase `maize-based subsistence farming` was classified as
`AgriculturalPractice` for the fumonisin claim and `Crop` for the ochratoxin
claim; the earlier two-call semantic gate then rejected the former. Three
other crop-contaminant claims stopped at source review because the extractor
omitted farming/location setting from an optional qualifier. The external
judge also incorrectly treated an empty hard-coded toxin-alias list as a
reason to reject a sentence naming `trichothecene mycotoxins` for a broader
`mycotoxins` endpoint.

V23 removes all toxin-specific aliases from code. Ordinary singular/plural
forms expand literature queries, while quoted external evidence must preserve
the same named concept or explicitly identify the broader class. Semantic
screening is one Step 5 call for canonical entity types and project scope.
The classifier's different type guess is recorded but cannot veto an edge
whose extracted types already satisfy the structural relation shape and whose
alternative classification would make that same edge structurally impossible.
Source entailment now owns relation, direction, certainty, and exact evidence
alignment. The pipeline no longer runs NLI on source evidence; DeBERTa remains
an external-evidence contradiction veto. A deterministic claim-based retrieval
sentence replaces the model-generated HyDE passage, saving one OpenCode call
per externally checked claim. Regional crop-production settings are retained
as source provenance even when the extractor left `population` blank.

## Earlier validation change (v22)

The October 14 audit of `Crop health and its global impacts on the components
of food security` found that a valid source claim could fail for operational,
rather than scientific, reasons: the regional phrase `maize-based subsistence
farming in South Africa` was treated as a universal external qualifier, and
literature commonly reports a singular toxin or a named form where the graph
label is plural. For `has_contaminant` only, a farming/site phrase is kept as
source provenance (`source_context`) instead of blocking independent
crop-occurrence studies. The v22 attempt added toxin-specific mappings; these
were removed in v23 after the rerun showed they made general evidence matching
less reliable.

External paper decisions run with up to three bounded parallel Step 5 calls.
One timed-out document is persisted as `needs_review`; it no longer aborts the
other external papers for that claim. `summary.json` now records attempted
OpenCode request counts and elapsed request time, so run-time costs are
auditable per PDF.

## Earlier validation change (v19)

Every non-empty qualifier is a required retrieval anchor. Document selection
combines dense retrieval, BM25,
exact title-anchor coverage, and scholarly-search result rank before
passage-level entailment. This specifically prevents broad Fusarium reviews
from displacing an exact `Fusarium head blight + infected grains + mycotoxins`
study. These are selection signals only: Step 5 and the NLI contradiction veto
still require an exact quoted supporting passage.

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

Automatic starter-graph extraction relations are:

`affects`, `reduces`, `increases`, `has_nutrient`, `has_contaminant`, `contains`, `has_trait`.

`causes`, `leads_to`, `deficiency_causes`, `associated_with`, `higher_than`, and
`lower_than` remain deferred to an observational/causal evidence workflow.
Association must never be upgraded to a directional or causal relation. Model
confidence is metadata and is never an acceptance rule.

## Current correctness-first pipeline

1. Extract PDF text into a continuous section-aware sentence stream. Protect scholarly abbreviations such as `et al.` from false sentence breaks, detect conservative unnumbered topic headings, remove obvious publisher/front-matter noise, and form overlapping 4,000-word packets for LongCat extraction.
2. Classify document scope and text quality. Scope is advisory; unusable text blocks the run and requires OCR or a cleaner PDF.
3. Give OpenCode LongCat the full packet plus a ranked index of likely mechanistic sentences. Emit at most four claims per packet by default; zero is valid.
4. Require each claim to select one to three consecutive source sentences and exact copied subject, object, and relation-trigger mentions. Entity anchors must be distinct noun phrases and cannot contain the relation trigger; copying an entire proposition as both endpoints is rejected before further model calls.
5. Preserve atomicity: wording such as `X forms M, which increases Y` may support `X -> M` and `M -> Y`, but never a collapsed `X -> Y` edge.
6. Clean only redundant qualifiers. In particular, a practice repeated in `crop_or_species` is removed and audited; genuine species, population, comparator, metric, dose, and condition restrictions remain.
7. Run structural schema validation, then one Step 5 semantic decision for project scope and canonical entity types. A type disagreement is recorded when the extracted type already forms a legal edge but the classifier's alternative would not. Relation, direction, certainty, and atomicity belong to source entailment.
8. Apply conservative vocabulary normalization. Exact reviewed mappings may receive ontology identifiers; fuzzy identifier invention is forbidden.
9. Run source entailment with a separate stateless Step 5 call plus deterministic span, surface, relation, direction, and modality checks. Full-sentence modal detection preserves `may`, `can`, and `could` as conditional. A generic mention such as `disease intensity` may bind to `False smut` only when the exact local section heading establishes that topic. Harmless domain modifiers are allowed, but different crops or materials are not.
10. Resolve both numeric citations and author-year citations from the exact source evidence span, including comma-free forms such as `Ding et al. 1997` and APA bibliography entries with parenthesized years. Resolve bibliography text through Crossref, then look up that exact work in Europe PMC or OpenAlex. A citation is only a retrieval lead and never support by itself; its selected full-text passage or abstract must pass entailment.
11. Search broad literature in both Europe PMC and OpenAlex. OpenAlex is necessary because agronomy coverage in Europe PMC alone is incomplete. The originating PDF is excluded from external corroboration.
12. Rank article metadata with BM25 and BGE-base using reciprocal-rank fusion, then rerank leading candidates with MedCPT. Prefer Europe PMC XML, then a transient OpenAlex public PDF, then an indexed abstract.
13. Ask Step 5 to judge every external document independently. It must select one or two consecutive sentences and match subject, object, relation, direction, certainty, and relevant qualifiers.
14. Deterministic evidence checks require exact quoted source mentions and relation cues, but accept a deliberately small lexical-normalization set such as microbial/microorganism, community/composition, and suppressiveness/suppression. They still reject material substitutions such as organic fertilizer versus suspension fertilizer.
15. DeBERTa NLI is only an external-evidence contradiction veto. It cannot create support or replace Step 5 as the evidence judge.
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

Version v14 fixed those systematic retrieval/alignment errors without weakening relation, direction, qualifier, or provenance requirements.

The October 11 `Crop health and its global impacts on the components of food security` run exposed a separate v14 extraction problem. All five claims passed schema and semantic validation, but three stopped at source entailment:

- `Fusarium head blight increases mycotoxins` overstates a sentence saying mycotoxins *may be produced in infected grains*. V15 directs extraction to the faithful edge `infected grains has_contaminant mycotoxins` with conditional certainty.
- `False smut associated_with hybrid rice cultivation` is supportable because `disease intensity` occurs inside the explicit `False smut of rice` section. V15 persists and validates that bounded section-topic binding.
- `False smut increases rate of milled rice decrease` collapses the section topic with the sentence's direct endpoints. V15 instead requires an edge between infected grains and milled-rice rate.
- The old splitter broke sentences at `et al.`, truncating citations and confusing the following sentence. V15 protects scholarly abbreviations.
- The old author-year resolver did not link forms such as `Parry et al. 1995` to APA references such as `Parry, D. W. ... (1995).`; V15 resolves those pairs so cited studies become targeted retrieval leads.
- A temporary broad LLM-only matching change could accept different materials such as organic and suspension fertilizer. V15 restores deterministic external entity and qualifier checks while retaining narrow lexical variants.

Existing v14 files do not change automatically; rerun the PDF to obtain v15 outputs.

The subsequent v15 rerun showed that `increased disease intensity` remained the graph subject even though its bounded heading named `False smut`. That generic label caused the exact Reddy et al. external passage to be rejected for a subject mismatch. It also repeated the disease-as-container error `Fusarium head blight has_contaminant mycotoxins`, despite the explicit container `infected grains`. V16 canonicalizes only generic disease-intensity labels from conservative local disease headings, repairs an explicit `contaminant produced/detected/found in infected grains` container error while retaining the disease as a condition, parses compact citations such as `Barnwal et al.2013`, adds a disease discriminator to external queries, processes explicit Europe PMC OA PDFs transiently, and rank-fuses MedCPT with dense/lexical retrieval instead of letting saturated cross-encoder scores dominate.

The v16 rerun retrieved a strong independent paper for `infected grains has_contaminant mycotoxins`, but Qwen sometimes cited a later sentence about `Fusarium spp.` rather than the direct sentence naming FHB-infected grains. The deterministic entity guard correctly rejected that mismatched citation, producing a false review despite good evidence elsewhere in the same chunk. V17 focuses the external judge on exact claim-bearing sentences when a sentence names both endpoints and a compatible relation cue; it falls back to all sentences when no exact lexical match exists. It also recognizes explicit toxin production or increases in crop material as `has_contaminant` evidence. This focuses evidence selection without allowing a related pathogen to substitute for the crop-material subject.

V18 addresses external-evidence coverage rather than weakening entailment. A single literal query can miss agricultural studies whose abstract reports an association as `incidence`, `severity`, `occurrence`, `prevalence`, or `susceptibility`. Each claim therefore gets one planned retrieval pass with: (a) Qwen's bounded literal concept query and (b) one deterministic relation-measurement query. Europe PMC and OpenAlex are searched with both variants, results are deduplicated, and every selected passage still goes through exact sentence selection, Qwen entailment, lexical entity/relation checks, and contradiction-only NLI. One source-cited paper is a separate lead; it no longer consumes the configured budget of independent external papers. This is not a validation retry and does not turn source-reference support into independent corroboration.

V18 additionally treats every non-empty candidate qualifier as mandatory at external judgement, including disease condition. Thus an oat/Fusarium-poae study cannot support a claim restricted to Fusarium head blight merely because both discuss infected grains and mycotoxins. The article-ranking stage promotes metadata that explicitly covers the subject, object, condition, and relation-measurement anchors before cross-encoder truncation. OpenAlex uses a compact literal endpoint/condition query plus a separate expanded query because its plain-text search can omit an exact-title study when given an overlong all-terms query.

The starter graph scope is intentionally narrower than the full ontology. Automatic extraction admits direct typed pathway/material edges only: `affects`, `increases`, `reduces`, `has_nutrient`, `has_contaminant`, `contains`, and `has_trait`. Association-only and comparative observations remain scientifically useful, but they require explicit study-design, population, geography, exposure, and comparator modelling before they can become general graph edges. For example, the cited Reddy study supports an association between false-smut intensity and hybrid use in surveyed Indian districts; it does not establish a universal `False smut associated_with hybrid rice cultivation` edge. These observations remain visible in archived/audit outputs for future observational modelling, preventing them from inflating automatic external-review counts or entering the main graph.

Europe PMC metadata calls have one bounded retry for transient `429` and `5xx` responses. This is a transport safeguard only: it does not repeat LLM extraction, query planning, judging, or evidence verdicts.

Verified v17 result for `Crop health and its global impacts on the components of food security`: five bounded claims were extracted; two passed semantic and source validation. `infected grains has_contaminant mycotoxins` received one independent external support and entered `graph.json` as `single_source`. `False smut associated_with hybrid rice cultivation` received exact cited-paper support and remains in the audit graph as `source_reference_supported` pending an independent article. The invalid milled-rice comparison and generic food-security endpoints did not enter the main graph.

## Evidence states and graph admission

- `source_supported`: faithful to the local PDF, but not yet externally supported.
- `source_reference_supported`: supported by a paper cited in the local source; useful, but not independent corroboration.
- `single_source`: at least one independently retrieved study supports the edge; included in the main external graph.
- `corroborated`: at least two independent publication/study groups support the edge.
- `not_found` / `external_evidence_not_found`: bounded retrieval found no acceptable evidence; this is not a false verdict.
- `contradicted` or `contested`: explicit conflicting external evidence exists.
- Semantic/source rejection and review states remain in the audit graph and never enter the main graph.

## Models and runtime

- Generative extractor: OpenCode `opencode/longcat-2.5-preview-free`; validation judge: OpenCode `opencode/step-5-preview-free`.
- Dense bi-encoder: `BAAI/bge-base-en-v1.5` on the automatically selected specialist device.
- Cross-encoder reranker: `ncbi/MedCPT-Cross-Encoder` on the automatically selected specialist device.
- NLI contradiction veto: `cross-encoder/nli-deberta-v3-base` on the automatically selected specialist device.
- `scripts/run_backend.sh` automatically moves all three specialist models to
  `cuda:0` when CUDA-enabled PyTorch is installed; otherwise it deliberately
  keeps them on CPU and points to `scripts/enable_gpu_models.sh`. This avoids a
  misleading GPU setting with a CPU-only PyTorch wheel.
- LongCat extraction calls and Step 5 validation calls are stateless; there is no rolling KV cache across packets.
- Environment overrides are in `scripts/run_backend.sh` and `external_validator/config.py`.

Start from Git Bash with two terminals:

```bash
bash scripts/run_backend.sh
bash scripts/run_frontend.sh
```

Backend code changes require a backend restart unless it was started with reload. Frontend changes require a Vite restart only when its running process does not hot-reload them.

## Important output files

- `candidates.jsonl`: canonical extracted candidates and source provenance.
- `extraction_rejections.jsonl`: extraction-stage discards with reasons.
- `schema_valid_candidates.jsonl`: structurally valid candidate IDs.
- `semantic_validation.jsonl`: scope and canonical entity-type decisions.
- `source_validation.jsonl`: local source-entailment decisions.
- `validation_runs.jsonl`: retrieval packets and per-external-document decisions.
- `graph.json`: independently externally supported edges only.
- `source_graph.json`: source-entailed edges.
- `audit_graph.json`: rejected, review, contradicted, and externally unsupported edges.
- `summary.json`: stage counts, model configuration, and graph counts.
- `run_log.jsonl`: frontend activity log.
- `evaluation_sample.jsonl`: immutable predictions with blank gold fields for independent evaluation.

## Measurement and next work

Acceptance rate is an operational diagnostic, not accuracy. A quality percentage requires labels independent of the extracting LongCat model. The generated stratified evaluation sample minimizes review work but intentionally leaves gold fields blank.

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

Immediate next step: rerun the representative PDFs with v19 and inspect `query_plan`, `query_result_counts`, selected documents, and their final evidence decisions. Keep source-reference-only support out of the main graph. Association-only disease-to-cultivation claims are not emitted by the starter extractor; they require a later observational evidence model with study-design, geography, exposure, comparator, and effect-measure metadata. Do not start path-level reasoning until edge-level precision and external-support coverage are stable.

## Key implementation files

- `backend/pipeline.py`: PDF packetization, extraction contract, citation/qualifier handling, orchestration.
- `backend/ontology_schema.py`: stable entity/relation vocabulary and legal pathway shape.
- `backend/semantic_validation.py`: Step 5 semantic gate.
- `backend/source_validation.py`: source-entailment and modality safeguards.
- `backend/vocabulary.py`: conservative entity normalization.
- `backend/graph.py`: evidence-state graph construction.
- `external_validator/workflow.py`: citation and broad retrieval, ranking, judgement, evidence accounting.
- `external_validator/retrieval.py`: Europe PMC, Crossref, and OpenAlex clients plus transient passage extraction.
- `external_validator/decision.py`: external Step 5 judgement and deterministic evidence invariants.
- `external_validator/rerank.py`: BM25/BGE RRF and MedCPT reranking.
- `external_validator/nli.py`: contradiction-only NLI check.

Reference slides: `ppt/Reasoning with Food Knowledge Graphs.pptx` and `ppt/Rag approach for knowledge graph validation.pptx`.
