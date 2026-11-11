# Established-only ontology grounding

The active flow is PDF parsing ? extraction (contract v10) ? grounding ? registry.
Manual **Run grounded claims** reuses completed saved extraction. Source fidelity,
external scientific validation, pathway assembly, graph construction and reasoning
remain paused. Grounding is representation assessment, not scientific validation.

## Inputs and decisions

All 203 approved established classes and 10 RO/BFO properties are supplied without
shortlisting. Class definitions, synonym categories and parent labels are upstream;
property definitions and available domain/range labels are retained. Provenance
URLs and hashes remain in the catalog rather than repeated in every model payload.
No locally authored scientific synonyms, definitions or relation shortcuts are used.

Each endpoint is assessed independently as exact, broader, narrower, related or
unresolved. Exact requires equivalent meaning, not simple inclusion in a class.
Relations are exact or unresolved. The five-field extraction output is unchanged
in shape; grounding never rewrites claims to fit the ontology.

## Grouping, review and identity

DeepSeek Flash uses Thinking On, five workers and groups of at most three sibling
claims with identical document/packet/evidence. Unrelated findings do not fill a
batch. Stable group and input signatures preserve checkpoint provenance. Changing
one member invalidates that group's compatible cache. Sibling conflicts are held
without overwriting the model's original mappings or copying scope decisions.

Clause-bound warnings for possible lost negation, restrictive quantities, defining
states and comparison populations create review holds. Entities are still grounded.
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
