# Pipeline stages

`backend/pipelines/document.py` controls stage ordering, checkpoints, failures
and final artifacts. Stage boundaries express ownership, not an extra model call.

| Stage | Main interface | Purpose and result |
| --- | --- | --- |
| `pdf_parsing` | `structure.inspect_pdf`, `packets.iter_pdf_context_packets`, `quality.assess_text_quality` | Parse chapters, bibliography links and literal sentences; report text quality |
| `claim_extraction` | `extractor.extract_claims`, `deduplication.deduplicate_source_claims` | Extract atomic claims with conditions, sentence anchors, citations and coverage |
| `source_validation` | `checker.validate_source_claim`, `review.*` | Assess PDF fidelity and project scope separately; preserve human review decisions |
| `pathway_assembly` | `assembly.assemble_pathways`, `paths.assembled_paths` | Align source-grounded entities globally and propose directed chains |
| `query_planning` | `neighbors.translate_pathway`, `planner.evidence_query_plan` | Plan neighboring-claim and junction queries; preserve neutral retrieval variants |
| `literature_retrieval` | `workflow.retrieve_round`, `clients.*` | Resolve citations, discover papers, fetch text and retain retrieval coverage |
| `evidence_ranking` | `articles._hybrid_order`, `passages.SemanticReranker`, `pathways.*` | Rank publications/passages and cover pathway retrieval targets |
| `neighbor_validation` | `workflow.assess_pathway`, `judgement.judge_paper`, `judgement.synthesize`, `proof_audit.audit_support` | Assess papers with DeepSeek Flash; Muse Spark 1.3 Contributor synthesizes and independently audits proposed local support |
| `external_validation` | `evidence.*`, `history.verified_contradictions` | Shared exact-text packing/quotation checks and historical evidence readers |
| `graph_construction` | `graph.graph_from_candidates`, `graph.write_graphml`, `graph.reasoning_paths` | Build source/audit snapshots and GraphML; retain historical corroborated health-path traversal |
| `evaluation` | `metrics.evaluation_sample`, `metrics.score_evaluation` | Produce independent annotation samples and evaluation metrics |

A relevance score selects text; it does not establish scientific truth. The
source model judges PDF fidelity and scope, while the external models judge
supplied research evidence for neighboring claims and local connections. Deterministic evidence checks
bind selected identifiers and literal text to their actual papers. NLI is not part of active execution.

## Shared ownership and historical code

- `claim_extraction/prompt.md` is loaded by its stage-owned prompt builder.
- `source_validation/prompt.py` owns source-fidelity prompt construction;
  `source_validation/scope.py` defines general project relevance.
- `neighbor_validation/judgement.py` owns active paper and synthesis prompts.
- `external_validation/evidence.py` numbers exact sentences, deduplicates repeated
  text losslessly and reconstructs proof. Historical proof guards remain readable.
- Scientific ontology, source alignment, context and modality helpers live in
  `backend/domain/`; infrastructure and providers have their own packages.
- Retired per-claim, joint-paper and trust executors are isolated under
  `archive/legacy_pipeline/research_historical/`. Production cannot import them; old test cases
  and research data remain intact.

Stages do not import API, CLI or pipeline entry points. Package imports do not
load models. Architecture tests enforce dependency direction and prevent cycles.

## Calls and scientific decisions

The active model calls are extraction (DeepSeek Flash), source/scope checking
(DeepSeek Flash; three independent claims per call by default), entity identity
assembly (MiMo Flash), local-link/junction query planning (MiMo Flash), paper
assessment (DeepSeek Flash) and cross-paper local synthesis (Muse Spark 1.3 Contributor). Two source
batches can run concurrently. The external Thinking switch controls both paper
and synthesis calls; all five stage switches default Off.

Parsing, hybrid candidate retrieval, article/passage ranking, adjacency selection,
exact proof reconstruction and graph publication do not each add a hosted LLM
call. BGE/MedCPT supply local ranking signals. Deterministic checks enforce the
response and evidence contract; they do not independently decide scientific truth.
