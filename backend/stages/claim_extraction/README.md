# Direct evidence-linked extraction

## Current contract and defaults

Contract v10 requires subject, object, relation, qualifiers and evidence_sentence_ids.
Optional subject_context/object_context retain endpoint restrictions; optional type
hints are provisional, not ontology identity. No claim_text, certainty labels or
copied passages are requested. Code retains exact original evidence and provenance.

DeepSeek Flash remains the primary model without fallback. Extraction Thinking is
Off, targets default to 200 words, two workers and a 50,000-token generation allowance.
Zero claims_per_packet means no numeric claim-count cap; generation remains finite.
Whole-packet mode remains available, alongside default target/context extraction.

## Target findings and supporting context

Targeting v5 uses policy bounded-sentence-supporting-evidence-v3. Each parsed
occurrence has one owning extraction target. Nearby context uses complete
sentences, a shared hard allowance of 250 words, and at most two neighboring
paragraphs per side within the same chapter. Closest sentences are selected
first, with antecedent-side priority on ties. A sentence that cannot fit blocks
that side; remote text is not substituted. A paragraph may be partially selected,
but a source sentence is never cut. Missing antecedents must not be guessed.

Prompts distinguish target finding IDs from support-only IDs. A claim must cite
at least one target sentence stating its finding; supplied context can support
antecedents and restrictions, not a separate finding. Never append an unrelated
target ID to satisfy ownership. An offline full-context comparison mode remains
available for research; it is not the active pipeline policy.

Response validation enforces valid JSON, required types/keys and unique ascending
supplied IDs. Unknown IDs remain structural errors eligible for bounded recovery.
Context-only findings are quarantined individually in extraction_rejections.jsonl,
with their original proposed claim and source IDs. They do not regenerate valid
siblings or abort a completed response. Coverage records expose the quarantine
count; processing completion is not exhaustive claim recall or scientific validation.

## Qualifiers and source anchoring

Only crop_or_species, population, condition, comparator and metric are allowed.
Use source-stated strings, preferably omitting unused keys. Explicit null in an
optional qualifier is accepted as absence and omitted from the stored qualifier
projection, never converted to the text "None". Numeric, object and list values
remain invalid. Raw model responses are preserved without edits or JSON repair.

Endpoint anchors must occur within a selected source sentence. Existing bounded
coordination and prepositional locators preserve literal source spans. Document-
defined abbreviation equivalents may also anchor, with definition provenance;
ambiguous/guessed expansions remain rejected. Matching can tolerate PDF line-wrap
hyphenation while retaining original offsets. No scientific synonym dictionary,
unrestricted fuzzy matching or deterministic scientific rewriting is used.

Atomicity must preserve joint causes, restrictive quantities, chemical/state
identity, modality, comparisons and numerical applicability. General clause-bound
review warnings can hold suspected omissions. They do not establish scientific
truth or restore a source-fidelity model stage. Grounding still runs on held claims,
but holds prevent automatic concept sharing and downstream eligibility.

## Saved runs and audit

Raw model artifacts, selected target/context IDs, exact fragments, citations,
anchor adjustments and review warnings remain inspectable. Contract and targeting
changes invalidate incompatible cached responses. Start a fresh extraction for
the new context policy; older completed artifacts remain preserved for audit.
The normal pipeline extracts then grounds. Manual Run grounded claims reuses a
completed extraction without fixing its scientific framing. All post-grounding
stages remain paused.
