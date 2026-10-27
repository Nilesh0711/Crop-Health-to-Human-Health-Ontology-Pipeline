# Validated assembly, extraction scope and full names (2026-10-26)

Pathway Assembly now requires source entailment, an explicit external in-scope
assessment, a completed independent-supported/corroborated result and accepted
independent supporting paper evidence. An unresolved, contradicted, pending,
source-review or source-reference-only edge cannot enter a chain. The API also
filters old snapshots against current eligibility while a rebuild is pending.
Changing this membership policy invalidates the corpus signature; unchanged
eligible entity alignments can be reused without another model call. Individual
edge support still does not establish causal compatibility of the whole chain.

The extraction prompt applies scope before returning an edge and includes six
short examples distinguishing annual wheat-loss statistics, yield/trade-only
claims, food-security summaries, biological processes, contamination and milling
recovery. Mixed sentences contribute only their eligible edges. This reduces
upstream noise without introducing a deterministic scientific-relevance gate
or replacing downstream scope judgement.

Source fidelity now checks and proposes PDF-defined abbreviation expansions in
the same source-model response. Only naming substitutions with exact source
definitions and a passing expanded-claim fidelity decision are committed. An
unknown abbreviation remains visible for review. Scientific meaning, modality,
conditions and exact source quotations cannot be rewritten by this mechanism.
See the [source-stage contract](../../backend/stages/source_validation/README.md).

Historical extraction/checkpoint data stays intact. Resume rechecks source
decisions under the new naming contract; a fresh run uses the new extraction
examples. The validated assembly cannot independently correct an external
judge's mistaken scope admission; that still requires evidence-based reassessment.
