# Project context: soil-to-human-health knowledge graph

## Goal

Build a provenance-rich research graph for this bounded pathway:

`Agriculture / Climate -> Soil -> Crop / Disease / Trait -> Nutrient / Contaminant -> Human Exposure -> Human Biological Process / Health Outcome`

The project values precise, evidence-backed edges over extraction volume. A graph path is a research hypothesis; it is not a newly proven direct causal fact.

## Application and storage

The project uses a FastAPI backend and React/Cytoscape frontend. Source PDFs live in `pdf/`. Each run writes derived data to `ontology/<PDF stem>/`. Full externally retrieved papers are never persisted; only selected evidence passages and bibliographic/provenance metadata are stored.

The default generative model is local Ollama `qwen3:8b`. It performs extraction and separate structured semantic, source, and external judgements. Retrieval and conservative verification use specialized non-generative models.

## Current correctness-first pipeline

1. **PDF quality and scope**: extract section-aware sentence packets, detect degraded text, and classify the paper as in-scope, upstream-only, out-of-scope, or uncertain.
2. **Evidence-first extraction**: process roughly 2,000-word packets. Each claim selects one to three adjacent source sentences and exact subject, object, and relation-trigger strings. Default output is at most four claims per packet, configurable from one to eight.
3. **Automatic relation policy**: accept bounded descriptive, directional, associative, composition, trait, and explicit comparison relations. Defer `causes`, `leads_to`, and `deficiency_causes` to a stricter causal-evidence workflow.
4. **Schema validation**: check only stable entity/relation labels and legal pathway shape. This is structural compatibility, not scientific correctness.
5. **Semantic validation**: use a fresh Qwen call to check project scope, entity types, relation strength, direction, and atomicity. Never silently rewrite direction, relation, scope, or atomicity.
6. **Vocabulary normalization**: perform conservative exact local mappings and record the recommended ontology family. ChEBI IDs are assigned only for a small reviewed exact dictionary; no fuzzy identifier invention.
7. **Source entailment**: use a stateless Qwen judgement plus deterministic exact-span anchors. The claim must preserve entity identity, relation direction, certainty, and qualifiers. DeBERTa NLI can veto an apparent entailment when contradiction is strong, but cannot promote a rejected or uncertain claim.
8. **External retrieval**: search source-cited studies and broad Europe PMC results. Fuse BM25 and BGE-large rankings with standard reciprocal-rank fusion, then rerank leading candidates with MedCPT.
9. **Evidence acquisition**: prefer Europe PMC XML, then an explicit OpenAlex OA PDF, then an indexed abstract. Remove reference/back-matter passages where possible and retain PDF page metadata. External files remain in memory.
10. **External judgement**: Qwen judges each document independently against exact subject, object, direction, relation, certainty, and qualifiers. DeBERTa NLI is a conservative conflict detector.
11. **Evidence accounting**: source-reference support is not independent corroboration. Count independent publication/study groups separately; two groups are required for the independently corroborated state.
12. **Graph views**: the main graph contains only independently supported claims. Source-supported and source-reference-only findings remain distinguishable. Rejected, uncertain, contradicted, and insufficient-evidence claims appear in the audit view.
13. **Evaluation artifact**: produce a status-stratified `evaluation_sample.jsonl` with immutable predictions and blank gold fields. Scoring reports strict triple/type/relation/source metrics and retrieval Recall@k/nDCG@k after independent labels are supplied.

## Important policies

- Different evidence spans are separate claim occurrences and are validated separately. Only exact overlapping duplicates are collapsed.
- Model confidence is metadata, never an acceptance criterion.
- Association cannot be upgraded to causation.
- Source faithfulness and world-level corroboration are distinct questions.
- `external_evidence_not_found` means bounded retrieval found no acceptable evidence; it does not mean the claim is false.
- `A -> B` plus `B -> C` does not establish `A -> C`.
- Paths and causal-composition validation remain deferred until edge extraction and validation have measured quality.

## Measurement

Stage counts and acceptance rates are operational diagnostics, not accuracy. A real quality percentage requires labels independent of the extracting model. The system prepares a small stratified review file to minimize manual work, but it deliberately leaves gold fields blank rather than letting Qwen grade itself.

Score a labelled run with:

```bash
python -m analysis.evaluate_pipeline score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

## Key implementation files

- `backend/pipeline.py`: orchestration and extraction contract
- `backend/ontology_schema.py`: controlled types, relations, and pathway shape
- `backend/semantic_validation.py`: semantic gate
- `backend/source_validation.py`: source-entailment gate
- `backend/vocabulary.py`: conservative ontology normalization
- `backend/evaluation.py`: review sample and metrics
- `external_validator/workflow.py`: scholarly retrieval and evidence accounting
- `external_validator/rerank.py`: BGE-large plus MedCPT two-stage ranking
- `external_validator/nli.py`: NLI contradiction check
- `backend/graph.py`: evidence-state graph construction

Reference slides: `ppt/Reasoning with Food Knowledge Graphs.pptx` and `ppt/Rag approach for knowledge graph validation.pptx`.
