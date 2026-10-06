# MVP RAG Pipeline for Extracted-Triplet Validation

## Decision and Scope

Build an **external, multi-source research-article validator**. It evaluates
whether a candidate triple is supported by independent external evidence.

The originating paper is retained as provenance metadata only. Source-paper
entailment is deliberately deferred to a later milestone.

The external validator answers:

> Do independent research articles support, contradict, or fail to establish
> the same relation in compatible context?

This provides a defensible basis for retaining a high-confidence KG edge. The
first version performs per-document entailment and simple, transparent evidence
aggregation. It deliberately defers source entailment, advanced cross-document
fusion, and global confidence modelling from
`ppt/Rag approach for knowledge graph validation.pptx`.

Out of scope for this MVP:

- systematic-review/guideline ranking and formal study-quality appraisal;
- source-paper entailment;
- advanced evidence fusion, learned global confidence scoring, or automatic
  contradiction resolution;
- automatic graph insertion and multi-hop path validation;
- replacing the project ontology wholesale.

The immediate goal is an auditable evidence ledger, not a black-box truth score.

## Do Not Build a Knowledge Base from Scratch

There are three distinct resources in this design. They must not be confused.

| Resource | MVP choice | Purpose | Does it prove a triple? |
| --- | --- | --- | --- |
| **Candidate provenance** | The project's own source PDFs, beginning with the current five papers | Retain the paper, tuple line, and file hash from which a candidate was generated | **No**; source entailment is deferred |
| **External evidence corpus** | Claim-specific research articles discovered through Europe PMC/PubMed and, later, agriculture-focused discovery such as PubAg | Test independent corroboration per document; retain only permitted full-text evidence | **Yes**, when an individual article entails the claim |
| **Terminology/ontology reference** | Existing public vocabularies, queried or cached as needed | Resolve aliases, propose canonical IDs, and check entity types | **No**; a vocabulary is not evidence for a scientific claim |

Do not build a massive knowledge base up front. Build a **claim-specific
evidence pack** on demand: retrieve perhaps 10–20 candidate research articles
for one triple, retain the relevant permitted passages and decisions, then cache
them. The external corpus is used for “is this independently corroborated?”;
source entailment may later answer “did the originating paper say this?”
without changing the externally validated evidence ledger.

### Reuse existing terminology; do not recreate it

Use only a small mapping cache for terms actually encountered in the corpus.
Do not download, curate, or insert a complete external ontology into the KG in
this phase.

| Concept family | Preferred reference | MVP use |
| --- | --- | --- |
| Agricultural practices, soil, crops, climate, plant disease | [AGROVOC](https://agrovoc.fao.org/) | Alias lookup and canonical agricultural concept IDs |
| Food/crop materials and food components | [FoodOn](https://foodon.org/) | Use only when AGROVOC cannot distinguish a crop/food concept |
| Nutrients, contaminants, pesticides, and other chemicals | [ChEBI](https://www.ebi.ac.uk/chebi) | Canonical chemical IDs and aliases |
| Human diseases | [Disease Ontology](https://disease-ontology.org/welcome/) | Canonical disease IDs and aliases |

AGROVOC is available as linked data and through downloadable/API access;
FoodOn is designed as a farm-to-fork ontology; ChEBI provides a curated chemical
ontology; and Disease Ontology provides standard human-disease concepts.
They are suitable reference vocabularies, but none should be treated as proof
that a causal or associative statement is true. [AGROVOC access](https://aims.fao.org/standards/agrovoc/access-agrovoc), [FoodOn overview](https://foodon.org/), [ChEBI downloads](https://www.ebi.ac.uk/chebi/downloads), [Disease Ontology release](https://github.com/DiseaseOntology/HumanDiseaseOntology).

Store a resolved mapping in a versioned file such as
`rag/entity_mappings.jsonl`:

```json
{
  "surface_form": "cadmium concentration",
  "canonical_label": "cadmium",
  "external_id": "CHEBI:22977",
  "reference": "ChEBI",
  "project_type": "ChemicalContaminant",
  "resolution_status": "reviewed",
  "ontology_version": "record release/date here"
}
```

The example identifier is illustrative; a real mapping must be checked against
the chosen version of the external vocabulary. Preserve the original phrase
and paper context even when a canonical ID is assigned.

## MVP Architecture

```text
Current raw 6-tuples + originating-paper metadata
             |
             v
external claim-specific research-article evidence pack
             |
             v
retrieve/rerank evidence separately for every document
             |
             v
LLM entailment assessor returns decision + exact quoted span
             |
             v
deterministic admission policy + review queue
             |
             v
candidate_for_corroboration_review | world_supported_single_article | world_contested | world_insufficient
             |
             v
auditable JSONL evidence ledger
```

The RAG validator is a **claim classifier**, not an answer generator. It must
not use background knowledge to repair a missing claim, invent a mechanism, or
change a correlation into causation.

## Proposed Files and Records

Keep the current `ontology/<paper>/raw_triplets.txt` files unchanged. Add a
new `rag/` area so the existing rule-based pipeline remains reproducible while
validation is developed.

```text
rag/
  documents.jsonl                 # originating-paper metadata and file hashes
  candidate_parse_errors.jsonl    # malformed six-tuples; no silent loss
  entity_mappings.jsonl           # reviewed alias -> canonical-ID mappings
  candidates.jsonl                # raw tuple converted to a claim record
  external_validation_packets.jsonl # external article metadata + selected chunks only
  external_evidence_ledger.jsonl  # grouped per-claim support/contradiction record
  entailment_decisions.jsonl      # one LLM/human decision per external document
  review_queue.jsonl              # ambiguity, high-risk causal claims, and conflicts
```

### Persisted metadata and external evidence fields

```json
{
  "document_id": "local-source-document-id",
  "document_role": "candidate_origin",
  "title": "paper title",
  "local_pdf_path": "pdf/paper.pdf",
  "file_sha256": "source-file hash",
  "source_tuple_file": "ontology/paper/raw_triplets.txt"
}
```

```json
{
  "evidence_document_id": "external-stable-id",
  "provider": "Europe PMC",
  "pmid": "...",
  "pmcid": "...",
  "doi": "...",
  "title": "external paper title",
  "publication_year": "...",
  "evidence_chunks": [
    {
      "chunk_id": "external-stable-id:chunk-4",
      "rank_score": 12,
      "text": "selected external evidence passage only"
    }
  ]
}
```

External full text is never stored. Europe PMC XML is held in memory only,
split into paragraphs/chunks, ranked, and discarded after the selected chunks
are written. Similarity/rank score is never sufficient evidence: a validator
must quote the exact supporting span from a stored selected chunk.

### Candidate claim record

Convert each current six-tuple into a record with a stable ID and its known
paper source. Do not trust the current LLM confidence as validation evidence.

```json
{
  "candidate_id": "uuid-or-content-hash",
  "source_document_id": "...",
  "subject": {"label": "cadmium", "type": "Nutrient", "canonical_id": null},
  "predicate": "causes",
  "object": {"label": "kidney damage", "type": "HumanDisease", "canonical_id": null},
  "extraction_confidence": 0.90,
  "raw_tuple": "<cadmium, causes, kidney damage, Nutrient, HumanDisease, 0.90>",
  "candidate_status": "pending_external_retrieval"
}
```

Later, the overloaded project type `Nutrient` should be split into more precise
types such as nutrient, chemical contaminant, and exposure. For the MVP, retain
the original project type and record a proposed reference mapping rather than
silently changing the meaning of an old triple.

## Retrieval Design

### External-first retrieval

The implemented MVP does not build a local vector database or download a large
literature corpus. For each candidate, it queries Europe PMC, selects a small
number of open-access research articles, reads their full text in memory, ranks
paragraph chunks by entity and relation-term overlap, then discards the full
text. Only selected chunks and bibliographic metadata are persisted.

This conservative baseline is transparent and inexpensive. Dense retrieval,
BM25, and rank fusion can later improve recall after a labelled evaluation set
shows that provider search plus lexical ranking misses relevant evidence.

### Query construction

The initial implementation creates one transparent query per candidate from the
subject, object, and relation variants. When reviewed aliases are available, it
can expand to several queries and merge their results. Example future queries:

```text
1. "cadmium" "kidney damage"
2. cadmium causes kidney damage
3. cadmium renal toxicity / kidney injury          # aliases, if resolved
4. cadmium cause induce associated                 # relation-aware lexical query
```

Search for research articles using the entity names, relation variants, and,
when available, important context such as crop, dose, species, and population.
Retrieve and assess evidence separately for every returned article; never blend
passages from several papers into one LLM context and call the result a
document-level decision. Alias expansion from `entity_mappings.jsonl` is the
next retrieval improvement.

Start external discovery with Europe PMC and PubMed. Europe PMC provides
programmatic literature metadata and open-access content; use PubMed to find
and filter article types. Retrieve full text only where reuse is permitted,
beginning with Europe PMC/PMC open-access material. Add PubAg later if the
evaluation shows inadequate coverage for soil, crop, or plant-disease claims.
The PMC Open Access corpus has licence and approved-access requirements, which
must be respected. [Europe PMC developer resources](https://europepmc.org/developers),
[PubMed search help](https://pubmed.ncbi.nlm.nih.gov/help/), [PMC text-mining
policy](https://pmc.ncbi.nlm.nih.gov/tools/textmining/), [USDA PubAg](https://www.nal.usda.gov/services/public-access).

## Entailment Validator Contract

### Input

- one candidate triple;
- its entity/relation definitions from the project schema;
- chunks from **one identified external paper only**, each with a chunk ID and
  text, plus article metadata;
- no other scientific “world knowledge”.

### Required structured output

```json
{
  "candidate_id": "...",
  "evidence_document_id": "...",
  "decision": "supported | contradicted | insufficient | needs_review",
  "subject_match": true,
  "object_match": true,
  "relation_match": true,
  "direction_match": true,
  "certainty_match": "explicit | qualified | absent",
  "evidence": [
    {
      "chunk_id": "...",
      "quoted_span": "short exact supporting or contradicting sentence",
      "stance": "supports | contradicts"
    }
  ],
  "rationale": "brief, evidence-grounded explanation",
  "validator_confidence": 0.0,
  "model_id": "pinned-model-name/version",
  "prompt_version": "v1"
}
```

### Who is the final judge?

Semantic entailment cannot be decided by string matching alone: `renal injury`
and `kidney damage`, for example, can refer to the same outcome. An LLM is
therefore used as a **constrained evidence assessor**, but it is not the sole
or final judge.

The final automatic classification is an explicit policy applied to the LLM's
structured result and stored evidence:

| Status | Deterministic admission rule |
| --- | --- |
| `candidate_for_corroboration_review` | At least two external research articles directly support the relation. A reviewer must still confirm that the studies are independent and context-compatible before the edge becomes high-confidence. |
| `world_supported_single_article` | One external research article directly supports the relation. Keep as provisional evidence; do not use as a high-confidence reasoning edge. |
| `world_insufficient` | No external article directly supports the exact relation, or the available evidence is insufficient. Do not add to the reliable KG. |
| `contested` / `needs_review` | Explicit contradiction, incompatible context, ambiguous modality, table/figure-only evidence, a high-impact causal health claim, or disagreement between validators. Human review is the final authority. |

Thus the LLM supplies a reproducible semantic judgement with citations; the
policy engine decides automatic admission; and a human/expert decides the
exception queue. A second independently prompted validator (preferably a
different model) may be required for causal/high-impact claims, but agreement
between two LLMs is still not proof. Calibrate the policy on a blinded
human-labelled sample and target high **accepted-edge precision** (for example,
at least 90–95%), rather than a high percentage of tuples passing.

### Decision rules

| Decision | Meaning | Action |
| --- | --- | --- |
| `supported` | A passage in this one document explicitly supports both entities and the same relation/direction | Record a per-document evidence decision; admission is decided by the policy above |
| `contradicted` | A passage explicitly states an incompatible relation or direction in the relevant context | Write result and send to review; never delete the candidate silently |
| `insufficient` | The retrieved text does not establish the exact claim | Preserve it in the ledger; it does not count as supporting evidence |
| `needs_review` | Ambiguous wording, conditional claim, table/figure-only evidence, type conflict, negation, or poor PDF text | Send to human review |

Important semantic rules:

- `associated_with` or a correlation does **not** entail `causes`, `leads_to`,
  `increases`, or `reduces`.
- A statement about a compound, crop, dose, population, species, or outcome
  does not automatically entail the broader unqualified label.
- “May”, “could”, “suggests”, and “in vitro” must be preserved as qualifiers;
  they generally require `needs_review` if the candidate claims a definitive
  causal effect.
- Absence of a supporting passage is `insufficient`, never `contradicted`.
- The validator must cite chunk IDs and quote text. A result without a cited
  evidence span is automatically `insufficient`.

### Validator prompt policy

Use a low-temperature model with a JSON-schema/structured-output response. Its
system instructions should say:

```text
Use only the supplied excerpts. Do not rely on outside knowledge.
Determine whether an excerpt entails the exact triple, including subject,
object, predicate direction, modality, and context. Correlation does not entail
causation. If no explicit quoted evidence supports or contradicts the claim,
return insufficient. Do not rewrite the claim to make it true.
```

Persist the raw prompt input, output, prompt version, and model ID (or hashes
of them when storage policy requires it) so every decision can be reproduced.

## Correct Integration with the Existing Pipeline

Do not alter `pdf_to_antology.py` in this external-validation milestone. Use
`external_validation.py` to add candidate metadata and retrieve external
evidence. It reads current tuples and records their originating-PDF metadata,
but does not inspect the source PDF's text:

```text
ontology/<paper>/raw_triplets.txt
  -> rag/candidates.jsonl
  -> Europe PMC search + permitted full text in memory
  -> rag/external_validation_packets.jsonl
  -> rag/external_evidence_ledger.jsonl
```

This permits direct before/after analysis of the 213 rule-filtered tuples and
does not confuse the old `validated_triplets.txt` (structurally valid) with
external evidence statuses.

### Current implementation and storage policy

`external_validation.py build-candidates` writes `rag/candidates.jsonl` and
`rag/documents.jsonl` from the current tuple files. It records a stable
candidate ID, raw tuple, source tuple file/line, originating-paper title, and
local source-PDF hash where available.

`external_validation.py retrieve --limit 5 --articles-per-candidate 5`
searches Europe PMC per candidate. It keeps the external full-text XML only in
memory, extracts the best ranked chunks, and writes only those chunks plus
bibliographic metadata to `rag/external_validation_packets.jsonl`. It never
writes an external PDF, XML document, or complete article into this repository.

`external_validation.py ingest-decisions <decisions.jsonl>` creates
`rag/external_evidence_ledger.jsonl` from one-document-at-a-time LLM or human
entailment decisions. The expected JSON contract is documented in
`prompt/External Entailment Validator Prompt.md`.

After the validator is evaluated, make the following controlled integration:

1. Rename the existing rule-filtered concept in documentation to
   **schema-valid**.
2. Make graph construction read `accepted_claims.jsonl` rather than raw
   or schema-valid tuples.
3. Persist evidence/provenance and validator outcome on graph claims; use a
   `MultiDiGraph` or reified claim records so distinct relations/evidence are
   never overwritten.
4. Restrict reasoning to asserted forward edges. This is a later change, not
   part of the entailment MVP.

Do not overwrite `raw_triplets.txt`, `validated_triplets.txt`, or the existing
GraphML during validation development. They are useful baselines for measuring
how much the evidence gate improves correctness.

## MVP Milestones

| Milestone | Deliverable | Completion criterion |
| --- | --- | --- |
| 0. Candidate metadata | `documents.jsonl` and `candidates.jsonl` from existing raw tuples | Every candidate has a stable ID and originating-paper metadata |
| 1. External evidence packs | On-demand Europe PMC retrieval plus selected chunks only | No external full article is stored; each packet has metadata and ranked chunks |
| 2. Per-document entailment | Structured decisions plus quoted spans | No `supported` result lacks a quoted external chunk ID and evidence-document ID |
| 3. Evidence ledger and evaluation | Ledger, small expert-labelled set, error report | Report external Recall@k, support precision/recall/F1, accepted-edge precision, and review rate |
| 4. Graph hand-off | `accepted_claims.jsonl` | Graph integration is approved separately |

Start the evaluation set with a stratified sample of supported-looking and
obviously doubtful tuples from all five papers. Include causal, associative,
increasing/reducing, and deficiency relations; include type-conflict and
generic-node cases. The required retrieval metric is **Recall@k of a human
identified supporting chunk**. The validator cannot be judged fairly if the
correct passage was never retrieved.

## Later Expansion (Explicitly Deferred)

After the external-validation MVP has acceptable measured performance, add in
this order:

1. Study-quality signals and priority for systematic reviews, meta-analyses,
   guidelines, and stronger primary-study designs. This is an evidence-quality
   feature, not a replacement for per-document entailment.
2. Formal cross-document fusion: source-independence clustering, evidence
   quality weighting, contradiction strength, and a calibrated global score.
3. Domain-KG relation checks as a **secondary signal** only. Existing KGs and
   ontologies may help discover aliases, type concepts, or known relations, but
   they do not replace cited research evidence for acceptance.
4. Source entailment as a separate provenance/fidelity layer.
5. Automatic graph insertion, confidence-aware forward-only reasoning, and
   reasoning-path validation.

Europe PMC provides APIs and open-access content/metadata for programmatic
work; PMC offers a large open-access full-text subset but licenses vary and its
approved programmatic-access routes must be followed. [Europe PMC developer
resources](https://europepmc.org/developers), [PMC text-mining policy and
datasets](https://pmc.ncbi.nlm.nih.gov/tools/textmining/).

## Non-Negotiable Guardrails

- Never manufacture a missing soil-to-health bridge to complete a desirable
  multi-hop path.
- Never treat ontology membership, embedding similarity, or LLM confidence as
  evidence of a scientific relation.
- Never treat several papers that repeat the same study, or several reviews of
  the same underlying evidence, as independent corroboration.
- Never use a domain-KG edge as evidence for accepting a research claim; it is
  a terminology/consistency signal only.
- Never upgrade association to causation.
- Never count an external article as supporting without a stored quoted chunk
  and bibliographic identifier.
- Never combine passages from different papers into one validator prompt and
  call its answer a document-level entailment decision.
- Never let later external evidence overwrite or hide a conflicting local
  record; retain both with their provenance.
- Keep every artifact versioned: source file hash, parser, chunker, embedding
  model, index, extraction prompt/model, validator prompt/model, and schema.
