# Neighboring-claim validation

Current policy: `neighbor-pair-v1`, application v50. See the
[active protocol](../../../docs/protocols/neighboring_claim_validation.md).
The [qualified evidence contract](../../../docs/protocols/qualified_neighbor_assessment.md)
versions semantic assessments separately from extraction. It adds proposition
match declarations, partial edge support, proof-bound correction proposals,
focused retrieval and separate scientific/execution status. Older local outcomes
remain readable but require refresh before current graph admission.

Inherited v3 rules retain strict applicability and one model-assessed
`connection_kind` per synthesis junction: mechanistic, contextual, exclusion or
unresolved. Only compatible mechanistic/contextual connections can grant local
support. Exclusion contrasts remain auditable. Exact means scientific equivalence,
not identical words; partial/contextual paper proof can ground a correction
without validating the unchanged original. See the
[v48 protocol](../../../docs/protocols/identity_atomicity_and_qualified_support.md).

| Module | Responsibility |
| --- | --- |
| `targets.py` | Directed adjacency, bounded local pairs, identities and invalidation |
| `findings.py` | Exact claim-paper finding reuse; no similarity-based verdict reuse |
| `retrieval.py` | Shared scholarly retrieval for links and their local junction |
| `judgement.py` | DeepSeek paper findings, Muse Spark 1.3 Contributor local synthesis, proof checks |
| `workflow.py` | Ready-paper sessions, checkpoints, shared queries and bounded follow-up |
| `records.py` | Local projections, manual evaluation samples and historical-result readers |

`backend/pipelines/neighbor_validation.py` owns bounded concurrent pair execution.
The original claims and literal evidence remain attached. Long assembled chains
are not externally validated as complete mechanisms. NLI and old trust workflows
are absent from active execution. Saved v45 whole-route results remain readable.

Retained proof echoes are idempotent only for the exact same chunk/sentences.
Conflicting selections remain excluded. After bounded recovery, evidence-selection
errors hold the affected pair as incomplete while other pairs continue; paper
successes and unadmitted proposals stay available for resume/audit. Provider,
storage and programming failures still propagate. Completion callbacks cannot
silently lose checkpoint errors or replace newer expanded paper packets.

External contract v4 adds `proof_audit.py`: Muse Spark 1.3 Contributor independently checks proposed supported pairs against their selected external quotations. Admission requires a passed audit bound to the current target, assessment and proofs. A failed audit can use the existing single follow-up; unresolved audits stay held. Optional literal-number arithmetic is computed from same-paper proof; units/applicability remain model decisions. See [v49](../../../docs/protocols/proof_audit_atomic_reextraction_and_layout.md).
