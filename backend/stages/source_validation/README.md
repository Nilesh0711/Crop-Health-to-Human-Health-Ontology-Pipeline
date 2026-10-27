# PDF source fidelity and full canonical names

`checker.py` uses the assigned source model in the existing small-batch call.
The model verifies the fixed edge against the selected PDF evidence and uses
bounded context only for explicit entity/reference identity.

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
an actual selected fragment. Invalid proof gets bounded same-model regeneration;
an unresolved proof problem remains visible for review.

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
- `expanded`: the model proposes exact before/after naming edits with the short
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
Updated endpoint normalization and ordinary claim signatures prevent query or
external evidence reuse for a changed claim. Existing source decisions without
the new `validation_policy` are rechecked on pipeline resume; extraction need
not be repeated just for this source-contract change.

The strengthened extraction-domain prompt applies to newly extracted claims.
Resume reuses completed extraction, so a fresh document run is needed to measure
whether the new examples reduce yield/trade/statistical extraction noise.
