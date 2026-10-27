# Pipeline stages

`backend/pipelines/document.py` controls stage ordering, checkpoints, failures
and final artifacts. A stage boundary does not introduce another model call.

| Stage | Main interface | Input and responsibility | Result |
| --- | --- | --- | --- |
| `pdf_parsing` | `structure.inspect_pdf`, `packets.iter_pdf_context_packets`, `quality.assess_text_quality` | PDF chapters, bibliography links, literal sentences and quality | Page audit and provenance-bearing context packets |
| `claim_extraction` | `extractor.extract_claims`, `deduplication.deduplicate_source_claims` | Packets and stage-owned prompt/schema; exact anchors and artifact checks | Claims, citation links and extraction audit |
| `source_validation` | `checker.validate_source_claim` | Fixed claim and original PDF evidence | Entailed, unsupported or review decision |
| `query_planning` | `planner.translate_queries`, `planner.evidence_query_plan` | Claim, qualifiers and bounded source context | Complementary translated/neutral index queries |
| `literature_retrieval` | `workflow.retrieve_round`, `clients.*` | Citation resolution, discovery, publication deduplication and fetching | Evidence documents, citation lifecycle and coverage |
| `evidence_ranking` | `articles._hybrid_order`, `passages.SemanticReranker` | Metadata fusion and BGE/MedCPT passage ranking | Selected papers/chunks and auditable scores |
| `external_validation` | `workflow.validate_candidate`, `joint.judge_evidence_pack`, `proofs.decision_errors` | Fixed claim and every selected paper packet | Joint Pro assessment, guarded quotations, contextual types and aggregate evidence state |
| `pathway_assembly` | `assembly.assemble_pathways`, `paths.assembled_paths` | All saved triplets, local source context and a global MiMo Flash entity registry | Original directed edges connected through shared entities; no causal-chain validation |
| `graph_construction` | `graph.graph_from_candidates`, `graph.reasoning_paths` | Saved claims and guarded decisions | Source/external/audit graphs, exports and provisional paths |
| `evaluation` | `metrics.evaluation_sample`, `metrics.score_evaluation` | Predictions and separately entered human gold fields | Annotation samples and metrics |

Retrieval uses ranking interfaces to select papers/passages. External validation
coordinates retrieval before the joint judgement. A relevance score does not
establish scientific truth. Graph/evaluation consume the resulting evidence
states. `backend/pipelines/claim_repair.py` reuses these stages while preserving
prior claims, revisions and unrelated findings.

## Resources and shared rules

- `claim_extraction/prompt.md` is the runtime template, loaded by `prompt.py`
  with controlled ontology definitions and extraction limits appended.
- `claim_extraction/contract.py` owns the response shape and lexical guards.
- `claim_extraction/targeting.py` assigns deterministic target regions and marks
  reading-only context. See [runtime extraction modes](claim_extraction/README.md).
- `external_validation/prompt_reference.md` documents the external contract;
  runtime instructions, schema and guards live in `joint.py` and `proofs.py`.
- Scientific types, quoted alignment, claim context, modality and literal search
  terms shared across stages live in `backend/domain/`.
- Providers, settings, storage, caching and batching have their own packages.
  Package imports do not instantiate models.
- Standalone scope/semantic helpers are removed; joint external judgement owns
  evidence-grounded type and pathway assessment.

Models, thresholds, proof requirements, JSON artifacts and API behavior are
preserved by this structural refactor. Regression tests cover those contracts;
architecture tests prevent reverse dependencies and import cycles.
