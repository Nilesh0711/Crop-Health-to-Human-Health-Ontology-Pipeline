# Direct evidence-linked extraction

## Current contract and defaults

Production uses Contract v26 and `closed-ro-bfo-12-v1`. The model returns
`subject`, `predicate_alias`, `object`, `qualifiers`, `evidence_sentence_ids`
and `relation_anchor`. The alias selects one of twelve established RO/BFO
properties (`P1`–`P12`) and determines the saved relation label and ID.
The relation anchor is a verbatim cue from a cited target sentence. Optional
endpoint anchors retain literal source spans.

Standalone observations are omitted when no approved property fits. Measurements
and comparison context for approved assertions stay in qualifiers. Explicit correlations
use P5 when source-stated sign and significance are preserved in `metric`.
The prompt prioritizes useful links in the soil-to-health journey within the
active scope. The Appendix includes the active prompt and examples.

For causal properties, the source must attribute the effect to the selected
subject. A condition recorded with the outcome stays in qualifiers, including
when the relation anchor quotes the entire observational clause.

Proposals with a free-text relation, observation payload or missing approved
property are recorded in the rejection log. Other valid findings in the same
response continue through anchoring.

DeepSeek Flash remains the primary model without fallback. Extraction Thinking
defaults to On. Targets default to 200 words, two workers and a 50,000-token generation allowance.
Pure measurement/time endpoints, source-defined treatment-code frames and
suspicious mechanism or comparison subjects can trigger review holds. Checks
run after verified abbreviation expansion and preserve the proposed wording.
Zero claims_per_packet means no numeric claim-count cap; generation remains finite.
Whole-packet mode remains available, alongside default target/context extraction.

## Target findings and supporting context

Targeting v6 uses policy `bounded-sentence-supporting-evidence-v6`. Each parsed
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
Keep predicates short and move experimental setting, values and comparisons into
these qualifiers. Retain defining organ, chemical form, deficiency, efficiency,
ratio or event in the endpoint. Split independently asserted outcomes, never
individual effects inferred from a joint treatment. Keep negation and hedging.
Use source-stated strings, preferably omitting unused keys. Explicit null in an
optional qualifier is accepted as absence and omitted from the stored qualifier
projection, never converted to the text "None". Numeric, object and list values
remain invalid. Raw model responses are preserved without edits or JSON repair.

Endpoint anchors must occur within a selected source sentence. Existing bounded
literal checks and new bounded coordination/prepositional locators preserve source spans. An anchor alone does not validate its endpoint: the actual endpoint must be literal, source-defined abbreviation-equivalent, or an unambiguous bounded coordination derivation. Document-
defined abbreviation equivalents may also anchor, with definition provenance;
ambiguous/guessed expansions remain rejected. Matching can tolerate PDF line-wrap
hyphenation while retaining original offsets. No scientific synonym dictionary,
unrestricted fuzzy matching or deterministic scientific rewriting is used.

Anchoring v2 permits at most two explicit coordination axes using bounded shared
prefixes/suffixes and multiword conjuncts. Verb-crossing and ambiguous derivations
remain rejected. Shared-word reconstructions must retain the full supporting
coordination in the supplied anchor. Failure records name the role and category.
Numeric/chemical punctuation is protected in the matching-only view. Raw text,
sentence boundaries and original offsets remain unchanged. Supporting antecedents
must be cited; no general coreference resolver is added.

Atomicity must preserve joint causes, restrictive quantities, chemical/state
identity, modality, comparisons and numerical applicability. General clause-bound
review warnings can hold suspected omissions. They do not establish scientific
truth or restore a source-fidelity model stage. Runs displays held claims alongside
unheld proposals and shows the source evidence and warning reasons.

## Saved runs and audit

Raw model artifacts, selected target/context IDs, exact fragments, citations,
anchor adjustments and review warnings remain inspectable. Contract and targeting
changes invalidate incompatible cached responses. Start a fresh extraction for
the new context policy; older completed artifacts remain preserved for audit.
Run pipeline stops after extraction. **Ground saved claims** in Runs reuses
all unheld records from compatible v22–v25 runs for entity-only grounding.
Assertions keep their predicates. Observations contribute only their endpoint
classifications and comparison assessment.
Pathway assembly, reasoning and validation are paused.

## Historical classification update

The v16 prompt emphasizes independent outcome lists, passive roles, inverse trends
and mechanisms versus populations, without changing anchoring v2 or sentence IDs.
Grounding interpretation v11 accepts exact, broader or unresolved relations using
the same 19 established properties. Classified coverage includes justified broader
alignments; reasoning eligibility still requires an exact predicate. The catalogue
profile v12 has 212 established classes, adding upstream GO:0005773 vacuole.
Upstream property comments and usage notes remain separate from definitions.
Grouping-only properties permit broader classification, never exact/direct use.
Later result clauses receive the same conservative mechanism review as earlier ones.
See [the v16 protocol](../../../docs/protocols/atomic_grounding_v16.md).
