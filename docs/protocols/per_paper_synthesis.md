# Per-paper external judgement and cross-paper synthesis (2026-10-26)

The Crop Health audit run had Thinking Off. Its missed-support examples already
contained positive joint-model conclusions, so enabling Thinking is not used as
the fix. `per_paper_synthesis` is now the default external judgement architecture;
`EXTERNAL_JUDGEMENT_MODE=joint` keeps the previous method available for comparison.
This configuration changes the judge signature, not the extraction checkpoint
version or the retrieval fingerprint.

## Calls and context

1. One DeepSeek Flash call evaluates one paper's selected passages.
   It records support/conflict/insufficiency/review, exact finding IDs, explicitly
   selected paper-local context, study design, conditions, outcome and limitations.
   Background/review statements are distinguished from the paper's own results.
2. Code reconstructs findings from their original chunk-local sentence IDs. The
   finding selects 1-3 sentences, consecutive or separated, up to 180 selected
   words in total. IDs are exact lists, never implicit range endpoints:
   `[4,6]` means only 4 and 6; `[4,6,8]` means only 4, 6 and 8; `[4,5,6]`
   explicitly selects the complete interval from 4 through 6. Separate fragments
   are visibly joined by `[NON-ADJACENT EVIDENCE]`, never presented as continuous
   source text. Intervening sentences are reconstructed separately as context.
   Separately selected definitions/conditions can also be non-adjacent.
   The model owns semantic equivalence of canonical names and source mentions.
   Code checks schema, echoed canonical values, declared match/mismatch decisions,
   exact quotes and IDs. It has no scientific synonym or abbreviation dictionary.
   The finding's endpoint mentions and trigger must be literal finding text;
   the alignment may instead quote an explicitly selected paper-local definition
   or antecedent with the model's `explicit_definition`/`context_reference`
   identity explanation. This accommodates a defined full name and an abbreviated
   finding without requiring their literal forms to coincide.
3. One MiMo Pro synthesis call receives every paper's finding record and the exact finding
   and selected/intervening context sentences, preserving their original IDs and
   labeling each as selected finding, selected context, or intervening context. It compares
   populations, dose, assays, restrictions and study origins. Original PDF context
   is separately marked as scope/identity context, never independent corroboration.
   Synthesis may flag a finding for review; it cannot manufacture or promote proof,
   reverse a paper's direction, or borrow a missing condition from another paper.

The v2 judgement policy invalidates old aggregate verdicts when reassessed, while
compatible v1 per-paper responses are reused and rechecked from their unchanged
sentence selections. Retrieval/extraction checkpoints and historical findings
are preserved. The optional historical `joint` comparison mode is unchanged.
The frontend exposes selected IDs, separate exact fragments and intervening
sentences; skipped text is context, not automatically selected proof.

Per-paper judgement uses `OPEN_CODE_EXTERNAL_PAPER_MODEL` (default
`opencode-go/deepseek-v4.1-flash`). Cross-paper synthesis uses
`OPEN_CODE_EXTERNAL_MODEL` (default `opencode-go/mimo-v2.6-pro`). The model is
fixed per phase; errors never cause substitution. DeepSeek requests native JSON
mode with application schema validation; Pro requests native schema formatting.
Changing the paper model invalidates paper assessments and aggregate verdicts,
while compatible retrieved passages and extraction/source checkpoints are retained.
Both phases share the SAME external Thinking
setting; Off remains Off. All stage toggles now default to Off; each can be enabled manually.
With N papers, a fresh assessment normally uses N+1 model calls. This improves
request isolation and recovery, but does not promise lower total latency or cost.
The compact final packet avoids repeating all original retrieved chunks. Every
paper remains represented; exceeding the input budget stops visibly.

## Proof recovery and persistence

`context_selections` is required for supported/contradicted paper decisions,
including an explicit empty array when the finding is self-contained. For
insufficient/needs-review decisions, omission means no additional context was
admitted; the original response is retained without filling in a model field.
Other required assessment fields remain required, and supplied context is always
checked. The paper provenance marker is `paper-local-context-v3`; compatible
completed responses remain reusable and are rechecked against their original IDs.

Malformed decisive proof selections in otherwise complete, schema-valid JSON
enter the existing provider response check as `evidence_selection_error`, not
`incomplete_response` (which is reserved for missing/truncated content).
The same model may regenerate once from the original paper input with the error
description. No local text correction or alternative-model substitution occurs.
Scientific mismatches do not trigger retries seeking a more favorable verdict.
The existing one-extra-call contract covers syntax/schema/proof recovery for
each model request; no additional proof-retry loop is added outside it.

If the final complete assessment still selects invalid proof, the request is
logged as a warning, `evidence_needs_review`. Its proposal is preserved unchanged
in `review_response`, never `accepted_response`. The paper guard excludes invalid
evidence from support and marks that paper `needs_review`; other papers and
synthesis continue. Invalid role-proof selections similarly leave classification
unresolved while preserving admitted paper findings. Dispositions must still
account for every paper. Malformed JSON, schema-invalid responses, truncation and
transport/provider failures remain terminal after their bounded recovery.
Two different literal endpoint substrings in the same selected finding do not
need identical spelling; both must be traceable and the model confirms identity.

Each completed paper is saved in the claim's external checkpoint with an input
signature covering claim, paper packet, policy, model and Thinking. Resume reuses
compatible records and reruns only missing papers and synthesis. A synthesis
failure preserves all completed paper results. Provider failures remain visible
pipeline errors, rather than scientific rejections. NLI remains diagnostic in this
mode and cannot promote or overturn the paper model's decision.

## Results and independence

Accepted paper findings stay visible if claim-wide type classification remains
unresolved. `external_support_needs_review` distinguishes this case from absence
of evidence. `external_assessment_needs_review` identifies proof/assessment review
without accepted support. Both are excluded from the validated graph/assembly.

`independent_document_groups` measures supporting publication groups;
`independent_groups` counts supporting original-result groups in the new mode.
Several introductions/reviews restating a study do not count as multiple new
experimental results. These origin labels are themselves model assessments,
not a guarantee of study/dataset independence.

Completed, source-entailed claims expose **Reassess external evidence**. It reuses
the existing external-only action, compatible retrieval and completed paper checks;
the original candidate and decisions are retained in revision history. It applies
the current runtime external Thinking selection to both new phases.

## Verification limits

`reports/paper_synthesis_2026-10-26/evidence_replay.json` records offline replay of
the existing stripe-rust, Fusarium/wheat-mycotoxin and brown-spot/photosynthesis
model proposals. Seven previously withheld paper findings survive the new proof
checks without new context selections or edits to quotations. This isolates a
guard improvement; it is not a live per-paper-model accuracy benchmark. Existing
run decisions and human gold labels are preserved, and no paid document rerun
is started during implementation.

`reports/paper_synthesis_2026-10-26/proof_selection_failure_replay.json` replays
the exact two responses that stopped Crop Health job
`31dc4a9908a84bbd887c80a8cd79e267`. Both finished with `stop`, Thinking was Off,
and neither was truncated. The repeated invalid proof now becomes review without
poisoning the provider client. The pesticide/food-safety proposal is not promoted
to support, and the original run artifacts are unchanged.

`nonadjacent_evidence_replay.json` in that report folder repeats this saved
response under the v2 selection policy. Sentences `[4,6]` reconstruct successfully,
sentence 5 is retained separately as context, and no provenance-selection errors
remain. Its separate model-declared metric mismatch still requires review; valid
indices do not override a scientific mismatch.
