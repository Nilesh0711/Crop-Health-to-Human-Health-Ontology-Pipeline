# PDF fidelity, project scope and manual review

`checker.py` uses DeepSeek Flash by default in source batches of three claims.
`prompt.py` owns the fidelity instructions; `scope.py` supplies project scope.
The same response records these as separate decisions.
The model verifies the fixed edge against the selected PDF evidence and uses
bounded context only for explicit entity/reference identity.

## Request batching

`backend/pipelines/document.py` queues only source claims that need assessment.
`backend/shared/batching.py` sends up to `VALIDATION_BATCH_SIZE=3` claims in one
DeepSeek Flash request; `SOURCE_WORKERS=2` permits two batches concurrently.
Both are backend environment settings, not per-claim external-validation modes.

For ten pending claims, the initial requests contain 3 + 3 + 3 + 1 claims: four
calls, with at most two in flight. Bounded formatting repair or regeneration can
add a call to an affected request. Compatible completed source checks are reused
on resume and do not enter the pending batches.

The response is keyed by short request-local IDs (`C1`, `C2`, `C3`). Code binds
those keys to the original candidate IDs. Each value contains a separate fidelity
and scope assessment; evidence cannot be borrowed from another claim. Identical
nearby context can be supplied once through a text reference without summarizing
it. Batching reduces request overhead, not the number of scientific decisions.

`context.py` retains numbered, verbatim sentences from the same parsed section
and chapter in the extraction packet. This gives the judge earlier definitions
and antecedents that a two-sentence window can miss. It does not add sentences
to the selected effect evidence or cross a detected section/chapter boundary.
The frontend exposes this context separately from the selected proof.

The model decides whether a phrase such as “this affected material” refers to
Disease D-affected Crop C material. These are abstract placeholders. Code checks the echoed endpoint,
the selected literal mention, and the quoted source definition/antecedent; it
does not demand that the entire composite canonical name occur word for word
in that definition. It never rewrites the model's binding quotation locally.
An entailed decision still requires agreement on relation, direction, modality
and claim text. Proof slots must quote the corresponding endpoint mention in
an actual selected fragment. Invalid proof can receive bounded same-model
regeneration; exhausted recovery stops visibly. Scientific ambiguity can instead
produce a valid `needs_review` decision, which remains available for manual review.

The prompt distinguishes effects assigned to different subjects, indirect
chains from direct edges, and threats/possibilities
from measured reductions. These are model instructions, not deterministic
scientific synonym or relation rules.

Runtime examples use placeholder entities rather than claims copied from a
particular paper. Real source cases remain in isolated tests and audit records.

Before returning its fidelity decision, the same response checks canonical
subject/object names, relation, claim text, source occurrence context and
qualifiers for abbreviations. `abbreviations.py` defines this response contract:

- `clear`: no name needs expansion.
- `expanded`: the model proposes naming edits with the short
  form, full name and a verbatim source definition containing both.
- `unresolved`: the supplied source does not establish an unambiguous expansion;
  the claim remains under review.

The model judges the expanded claim in the same call. Code checks that each
definition exists in that claim's supplied context and each proposed edit only
replaces the declared name. It never guesses a scientific expansion or writes
its own correction. All ordinary fidelity and proof checks still apply. Only an
entailed result commits the expanded names. IDs, evidence sentences, source
mentions, quotations and triggers retain their original text; source-defined
abbreviations may legitimately appear in that exact provenance. Units, chemical
formulae and formal gene/strain identifiers are preserved.

Already expanded names need no edit. Redundant unchanged annotations and
source-defined edits that have already been applied are idempotent: they do not
reject an otherwise faithful claim or create fictitious name-correction history.
Actual edits still require exact original values and an unambiguous source
definition of both forms.

Applied edits and definitions are retained in `source_name_corrections` and the
source result, displayed in claim details and logged by the document pipeline.
Updated endpoint normalization and ordinary claim signatures prevent reuse of
affected pathway evidence for a changed claim. Existing source decisions without
the new `validation_policy` are rechecked on pipeline resume; extraction need
not be repeated just for this source-contract change.

The strengthened extraction-domain prompt applies to newly extracted claims.
Resume reuses completed extraction, so a fresh document run is needed to measure
whether the new examples reduce yield/trade/statistical extraction noise.

## Scope and manual decisions

The project covers soil/crop/food/composition/contaminant/exposure/human-health
relationships. The model assesses the atomic edge in its actual source context;
there is no paper-name rule, keyword scope gate or relation/type-pair whitelist.
An upstream biological segment need not state a health endpoint. A faithful
out-of-domain assertion can be `entailed` and `out_of_scope` at the same time.

Provisional assembly requires structural validity, effective fidelity `entailed`
and explicit scope `in_scope`. Unresolved/excluded claims retain their evidence
in audit views. `summary.py` supplies shared counts; PDF-supported counts and
assembly-eligible counts intentionally differ.

There is no automatic second scientific source assessment. The frontend offers
separate Accept/Reject controls for fidelity and scope. `review.py` binds each
manual decision to its claim/context/original assessment; the pipeline persists
`source_review_history.jsonl`. Changed inputs invalidate stale decisions. Original
model readings remain intact. These actions make no LLM calls and do not grant
external support; continue/assess saved pathways to rebuild affected routes.

Source-defined abbreviation proposals are checked through `output.py` and
`abbreviations.py`. Request-bound canonical fields and original edit `before`
values are reconstructed from fixed inputs; endpoint proof anchors reuse the
selected literal mentions. Explicit returned values are still checked. This
avoids duplicate output without inferring scientific meaning or dropping evidence.

Compatible completed source/manual work can resume. Old policies or changed input
signatures require a refreshed source call, with the prior assessment retained;
that compatibility refresh is separate from a retry seeking a favorable verdict.
The active policy is `source-fidelity-and-project-scope-v5`. It additionally checks
that the restatement does not bundle unrelated outcomes or authorial commentary.
Actual restrictions, quantifiers and meaningful source typologies are preserved.
No original claim is silently rewritten; atomicity problems remain reviewable.
Provider formatting/
missing-content recovery remains bounded and separate from scientific review.

## Compact source response

Empty occurrence_provenance_fields can be omitted. Abbreviation status remains
explicit: clear needs no empty arrays, expanded requires nonempty corrections,
unresolved requires a nonempty unresolved list. A same_concept alignment can omit
evidence when it only repeats its literal mention; code reuses that mention.
Coordination, explicit definitions, contextual references and different entities
still require separate evidence. Explanations, all four match flags, scope and
relation proof remain. Explicit returned values are never overwritten. Internal
records and scientific checks stay compatible with completed source assessments.

Source policy v5 explicitly requires inspecting sparse sentence selections and their same-section referents; non-adjacency alone is not a scientific rejection. Held compound claims can be deliberately re-extracted into source-checked children, preserving the original. See [v49](../../../docs/protocols/proof_audit_atomic_reextraction_and_layout.md).
