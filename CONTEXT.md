# Project Context: Crop-to-Human-Health Knowledge Graph

## Purpose

Build an evidence-backed knowledge graph (KG) from agricultural and health
research. The KG should let a researcher explore scientifically supported links
from agricultural conditions and interventions to human-health outcomes, for
example:

```text
farming practice -> soil/crop/plant disease -> nutrient or contaminant -> human-health outcome
```

The long-term value is *traceable hypothesis discovery*, not just producing a
large number of graph paths. A displayed claim or path must say what paper and
text support it, how strongly it was validated, and whether it is causal or
only associative.

## Research Intent

The project combines two domains that are normally disconnected:

- Agriculture: soil health, climate, crop management, plant disease, yield,
  food quality, nutrients, contaminants.
- Human health: deficiency, toxicity, exposure, disease, and health outcomes.

The initial presentation, `ppt/Reasoning with Food Knowledge Graphs.pptx`,
defines the target: automatically extract structured knowledge from literature,
merge papers into one cumulative graph, and reason across multiple hops. The
second presentation, `ppt/Rag approach for knowledge graph validation.pptx`,
defines the necessary next step: validate candidate knowledge against local and
cross-document scientific evidence before admitting it to the reliable KG.

## Current System (as of 2026-10-05)

### Corpus and outputs

Five papers have been processed. The current corpus contains 319 raw tuples,
of which 213 pass the existing rule-based filter. The committed master GraphML
has 211 nodes and 412 edges (forward plus synthetic reverse edges).

| Paper | Raw | Rule-filtered |
| --- | ---: | ---: |
| Crop health and its global impacts on the components of food security | 64 | 46 |
| Exploring the link between soil health and crop productivity | 69 | 39 |
| Integrated Soil and Crop Management in Organic Agriculture | 68 | 65 |
| The links between agriculture and health | 63 | 36 |
| agronomy-12-02947 (rice false smut) | 55 | 27 |

Input PDFs are under `pdf/`; per-paper tuples and graph artifacts are under
`ontology/<paper name>/`. The detailed tables and the earlier reasoning export
are under `deliverables/`.

### Existing code flow

`pdf_to_antology.py` is the operational pipeline (the filename contains the
typo `antology`). It **does not read PDFs**. A person uploads each PDF to an
LLM, uses `prompt/ChatGPT Extraction Prompt`, and saves the resulting lines to
`ontology/<paper>/raw_triplets.txt`.

```text
PDF --manual LLM extraction--> raw_triplets.txt
  -> exact line deduplication
  -> 6-field parse + type/relation/domain-range rules + LLM confidence >= 0.70
  -> per-paper NetworkX graph and cumulative master GraphML
  -> simple-path traversal (up to 6 hops) -> reasoning_output.txt
```

`visualize.py` reads the master GraphML and writes `graph_report.txt` and
`reasoning_paths_report.txt`. `README.md` and `pipeline.md` describe this
workflow. `requirements.txt` currently lists only NetworkX.

### Current tuple schema and ontology

The only persistent input record is a six-tuple:

```text
<subject, relation, object, subject_type, object_type, confidence>
```

The controlled ontology presently has seven entity types:
`Crop`, `PlantDisease`, `Nutrient`, `SoilCondition`, `ClimateFactor`,
`HumanDisease`, and `AgriculturalPractice`.

It has eight relations: `affects`, `causes`, `reduces`, `increases`,
`has_nutrient`, `leads_to`, `associated_with`, and `deficiency_causes`.
`VALID_RULES` specifies allowable type-relation-type triples;
`ENTITY_NORMALIZATION` maps a small set of surface forms to strings.

## What “validated” currently means — and does not mean

The current `validated_triplets.txt` label means only that a tuple:

1. has the expected syntax and allowed types/relations;
2. has an LLM-supplied confidence of at least 0.70; and
3. matches a hand-written domain/range rule.

It is **not** evidence validation. No sentence, page, section, DOI, study
context, source citation, retrieval result, entailment result, contradiction,
or validated confidence is stored. A rule-valid edge therefore must not yet be
described as scientifically verified or causal.

The current extraction prompt is also biased toward over-generation: it asks
for “as many tuples as possible”, to “aim for 50+”, and to “MUST include”
agriculture-to-health links. The replacement prompt must instead require only
claims directly stated in the source and require a verbatim supporting span for
each candidate. It must allow abstention when a bridge is not in the paper.

## Observed Quality and Modelling Problems

- **Unsupported or forced claims:** LLM self-confidence is the only confidence
  signal, and the source prompt encourages filling missing links.
- **No provenance:** the graph cannot show why any edge exists or be audited
  against its source PDF. Existing raw files cannot be fully repaired without
  re-extraction because their supporting text was never saved.
- **Ontology is both narrow and overloaded:** scientifically plausible tuples
  are rejected because needed concepts or domain/range rules are absent (for
  example `PlantDisease -> produces -> Contaminant`, or `SoilCondition ->
  reduces -> Crop`). Conversely, concepts such as toxin, contaminant, exposure,
  yield, food access, economic loss, and health status are forced into the seven
  types, where their meaning is distorted.
- **Type conflicts exist:** the same normalized entity is used with different
  types (examples: `crop diversity`, `food quality`, `grain quality`,
  `mycorrhiza`, `nutrient availability`, and `pesticide exposure`). The first
  type inserted into NetworkX wins silently.
- **Lossy graph storage:** `nx.DiGraph` permits only one edge for a directed
  node pair. Different claims such as `cadmium causes kidney damage` and
  `cadmium associated_with kidney damage` overwrite each other. With 213
  accepted tuple rows, two edges per row would be 426 edges; the current master
  has 412.
- **Evidence and confidence are discarded during graph construction:** GraphML
  edge attributes contain only `relation`; node attributes contain label/type.
- **Reasoning can reverse causality:** forward and synthetic reverse edges are
  placed in the same graph, and `all_simple_paths` traverses both. A reported
  source-to-disease path can therefore include `reduced_by`, `affected_by`, or
  another backward traversal. A path is not necessarily a causal chain.
- **Generic bridge nodes amplify false paths:** terms such as `crop`,
  `nutrient`, `nutrition`, and general human disease can join unrelated papers.
  Current normalization even maps `nutrition` to the generic `nutrient` key.
- **Master graph is append-only:** rerunning after a correction does not remove
  stale edges. A reproducible graph must be rebuilt from versioned accepted
  claims, not incrementally mutated as the source of truth.

The 67% rule-filter pass rate is useful as a structural diagnostic; it is not
an extraction-accuracy or scientific-validity metric.

## Target Validation Architecture

Keep the existing pipeline's good separation of extraction, structural checks,
graph building, and reasoning. Insert an evidence-validation layer between
candidate extraction and graph construction, as proposed in the RAG deck.

**Current implementation decision:** build external research-article validation
first. The originating PDF is retained as candidate provenance (paper, tuple
line, file hash), but source-paper entailment is deferred. The first reliable
edge decisions must therefore cite external evidence passages, not the
originating paper merely because it produced the candidate.

```text
PDFs / scholarly corpus
  -> candidate extraction + originating-paper metadata
  -> structural/type/domain-range validation and entity resolution
  -> retrieve a small external, claim-specific set of permitted research papers
  -> rerank by claim/relation/context alignment
  -> entailment validator: supported | contradicted | insufficient
  -> cross-document evidence fusion + calibrated confidence
  -> accepted / provisional / rejected claim store
  -> evidence-aware KG
  -> forward-only, confidence-aware path generation + path validation
```

### Claim-first data model (the critical change)

Use JSONL or a small SQLite database as the source of truth; GraphML is an
export/view, not the evidence store. A candidate claim needs at least:

```text
claim_id, subject_id, subject_label, subject_type,
predicate, object_id, object_label, object_type,
source_document_id, doi, paper_title, page, section, chunk_id,
supporting_quote, extraction_model, extracted_confidence,
ontology_status, retrieval_results[], evidence_assessments[],
support_count, contradiction_count, final_confidence,
status, created_at, pipeline_version
```

Also store qualifiers whenever present: crop/species/variety, population,
exposure route and dose, geography, outcome measure, study design, direction,
and modality (observed, associated, caused, hypothesised). These prevent claims
from different contexts being merged merely because their surface strings match.

Use stable entity IDs and an entity-resolution table with aliases, mappings,
type, and review status. Never normalize a term solely by replacing its text
with a broad concept such as `nutrient`.

### Validation gates

1. **Candidate provenance now; source entailment later.** Preserve source paper,
   tuple line, and source-file hash for every candidate. Do not call this
   evidence. A later source-entailment layer will attach/verify local spans.
2. **Structural and ontology validation.** Check syntax, non-empty entities,
   entity type, relation direction, and domain/range. Expand the ontology only
   after examining failed candidates; do not relabel concepts merely to pass a
   rule. Likely additions include `Chemical/Contaminant`, `Exposure`,
   `CropTrait/Quality/Yield`, `HealthOutcome`, and relations such as
   `produces`, `contains`, and `exposes_to`.
3. **External evidence retrieval and reranking.** Retrieve a small,
   claim-specific set of independent research articles through Europe PMC/
   PubMed; use permitted open-access full text only. Keep full text only in
   memory, persist selected evidence chunks and bibliographic metadata, and
   rank passages by entity match, relation/direction, and compatible context.
4. **Evidence assessment.** Have a validator return `supported`,
   `contradicted`, or `insufficient`, with a short rationale and cited spans.
   Missing retrieval evidence is `insufficient`, never proof of contradiction.
5. **Cross-document fusion.** Group aligned claims by canonical IDs,
   predicate, direction, and relevant qualifiers. Count independent supporting
   and contradictory sources; preserve both rather than overwriting the edge.
   Compute a final score later from independent support, evidence
   quality/context alignment, ontology status, and contradiction strength.
   Choose thresholds only after calibration on a human-labelled sample.
6. **Decision policy.** Keep one externally supported article as provisional;
   require two independent direct external supports plus review before a claim
   is high-confidence. Route conflicts/high-impact claims to review and retain
   rejected candidates for audit and error analysis.

## Graph and Reasoning Requirements

- Use `nx.MultiDiGraph` (or explicit reified claim/evidence nodes) so multiple
  predicates and evidence bundles between the same entities survive.
- Store **only asserted forward claims** in the reasoning graph. Generate
  inverse labels only as a display/query view, never as traversable causal
  evidence.
- Keep causal, influence, and association paths distinct. `associated_with`
  should not silently compose into a causal conclusion.
- Reason over accepted claims with a configurable confidence threshold; score a
  path from its weakest edge plus relation semantics and evidence diversity.
- Reject paths with generic hubs, incompatible type transitions, repeated
  entities, contradictory edges, or reverse traversal. Show all edge citations
  and the aggregate path confidence. Treat novel multi-hop outputs as
  hypotheses unless the full mechanism is independently established.
- Rebuild a versioned master graph from the accepted-claim store for each run.
  Do not use the current mutable master GraphML as the canonical database.

## Practical Integration Plan

1. **Create candidate metadata.** `external_validation.py build-candidates`
   records stable candidate IDs, raw tuple, source tuple file/line, originating
   paper, and PDF file hash for the existing corpus.
2. **Use the corrected extraction prompt.** It no longer targets a tuple quota
   or forces soil-to-health bridges; it preserves relation strength/direction.
3. **Retrieve external evidence on demand.** `external_validation.py retrieve`
   searches Europe PMC for a small set of permitted open-access research
   articles per candidate, keeps full text only in memory, and persists only
   selected evidence chunks plus bibliographic metadata.
4. **Run external per-document entailment.** Use the structured contract in
   `prompt/External Entailment Validator Prompt.md`; each decision must cite a
   stored chunk from one external article. Ingested decisions become an
   auditable external evidence ledger.
5. **Evaluate external validation.** Create a stratified human-labelled set;
   measure retrieval Recall@k, evidence-entailment precision/recall, accepted
   edge precision, and review rate before admitting claims to the KG.
6. **Add source entailment later.** It will test whether the originating paper
   actually stated the candidate, but will not overwrite external evidence.
7. **Replace graph construction and reasoning only after validation.** Build the reliable
   `MultiDiGraph` only from accepted claims, preserve provenance and confidence,
   and run forward-only path validation. Continue exporting GraphML for Gephi /
   Cytoscape compatibility.

## Repository Map

| Path | Role |
| --- | --- |
| `pdf_to_antology.py` | Current rule-based validation, NetworkX construction, and reasoning |
| `visualize.py` | Current GraphML reports and path listing |
| `prompt/ChatGPT Extraction Prompt` | Current manual extraction prompt; replace for evidence-grounded output |
| `ontology/` | Raw/rule-filtered tuple files, per-paper GraphML, current master graph/reports |
| `pdf/` | The five source PDFs |
| `deliverables/` | Snapshot tables, schema, and earlier reasoning export |
| `ppt/Reasoning with Food Knowledge Graphs.pptx` | Original problem, baseline pipeline, and progress |
| `ppt/Rag approach for knowledge graph validation.pptx` | Proposed evidence/RAG validation design |

## Guardrails for Future Agents

- Do not call existing `validated_triplets.txt` scientifically validated.
- Do not add a fact simply because it completes a desired soil-to-disease path.
- Do not silently merge entities with conflicting types or contexts.
- Do not traverse synthetic reverse edges as if they support forward causality.
- Do not discard evidence, conflicts, qualifiers, model versions, or rejected
  candidates; reproducibility and auditability are core project requirements.
- Preserve the current artifacts unless a migration is explicitly requested.
