# Established-only ontology grounding

**Run pipeline** stops after [bounded extraction v25](../claim_extraction/README.md).
Use **Ground saved claims** in Runs. This manual action reads all unheld claims
for the paper, retrieves up to 14
ontology candidates per endpoint, and sends one claim per call with up to five
calls in parallel. Each assertion keeps its Stage 1 property. Historical v23
measured observations can still receive endpoint mappings and stay outside the Relation registry.
The grounding model
uses Thinking On by default. Results appear in Mapping Review. Older mappings
remain available for inspection.

When an endpoint names an experimental arm, Stage 2 can include its verbatim
methods sentence from the same parsed PDF. A unique explicit label links the
endpoint to the setup. The original predicate, wording and evidence remain fixed.

Appendix shows the active entity-only prompt and the historical hybrid prompt.
Pathway assembly, merging, reasoning and scientific validation are paused.

## Hybrid retrieval reference (grounding v17)

The v17 packet supplies up to fourteen candidates per endpoint, ranked against the claim wording. Explicit cultivar declarations can supply a species candidate with its source passage. Its relation menu includes up to ten lexical matches from 101 reference properties. The supplied class ancestries identify which shortlisted endpoint aliases satisfy each property's named domain and range. Extended properties require verified compatible endpoint types. Class definitions, synonym categories and parent labels are upstream;
property definitions, separate usage notes/comments, upstream inverse axioms, IAO:0000589 labels and available domain/range labels are retained. A missing inverse definition is not manufactured. Provenance
URLs and hashes remain in the catalog rather than repeated in every model payload.
No locally authored scientific synonyms, definitions or relation shortcuts are used.

The shortlist removes candidates that fail the known quantity, analyte or
scale checks before ranking the remaining pool. Treatment-code analyte checks can use the verified naming sentence
attached to that exact endpoint. The dose remains in the source record and an
admissible exposure class stays broader.

The relation extension uses the content-addressed RO release of 4 September 2026.
Its candidate pool contains named, active RO properties with upstream labels and
definitions and named domain and range constraints. Properties with curator
notes were excluded from this expansion, including notes in the earlier
fragments. The negative process-to-characteristic sibling uses inherited
constraints. The 24 previously approved properties retain their existing usage
rules. Candidate rank nominates a property for the model to inspect. A new
property must also pass the existing evidence checks and have compatible
endpoint types before the v17 grounder accepts it.

Each endpoint is assessed independently as exact, broader, narrower, related or
unresolved. Exact requires equivalent meaning, not simple inclusion in a class.
Relations are exact, broader or unresolved. A broader relation is classification,
not reasoning admission; grouping-only properties cannot be exact. The five-field extraction output is unchanged
in shape; grounding never rewrites claims to fit the ontology.

## Grouping, review and identity

Manual grounding uses DeepSeek Flash with Thinking On by default.
One claim goes into each request and up to five requests run concurrently.
Stable input signatures preserve checkpoint provenance. When Stage 2 maps the
same anchored endpoint in the same context to the same class and differs only
on exact versus broader, it uses the supported broader alignment and retains
the original model decision in Mapping Review. Disagreements about the class or
denotation, and disagreements involving a manual mapping, still require review.
If a multi-claim request still has an invalid or incomplete response after the
provider's bounded recovery, the scheduler retries each claim in its own packet.
Each packet uses a fresh candidate list and the same strict validation. A failed
single-claim request or provider outage stops grounding with completed checkpoints
available for resumption.

Clause-bound warnings for possible lost negation, restrictive quantities, defining
states, pure measurements/time endpoints and suspicious mechanism subjects create
review holds. Entities are still grounded. Experimental manipulation does not turn
a concentration quality into an exposure event, nor efficiency into uptake.
Accept/Reject extraction requires a note and current revision; acceptance approves
only the extraction. Mapping conflicts require separate mapping review. Changed
claims invalidate prior extraction acceptance.

Only exact generic concepts without endpoint restrictions or identity-bearing
population/condition/comparator/metric qualifiers can share concept IDs. Held or
rejected claims cannot share identities or become assembly-eligible. Samples,
treatments, cultivar measurements and non-exact mappings retain occurrence IDs.
UI aggregation of repeated names is not a scientific merge.

Document summaries count their own current records; global registry counts are
reported separately. Changed policies/catalogs leave older records stale and
inspectable. No silent reassessment or full-PDF rerun is performed by an upgrade.

## Verification

Use the focused structural and review regression tests before live comparisons.
Real PDF cases are isolated in analysis fixtures, never runtime prompt rules.
Measure fidelity, erroneous exact mappings, missed findings, holds and latency
separately. Independent human review is required before claiming scientific accuracy.

## Historical acceptance and catalogue pilot

The [v18 acceptance protocol](../../../docs/protocols/atomic_grounding_v18.md)
records the earlier review thresholds and the isolated 19-versus-21-property
pilot. Its fixed formulations serve as diagnostics. Keep full-PDF application
counts separate from source-reviewed rates.
