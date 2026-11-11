# Frozen established ontology reference

Profile v9 selects **203 established classes and 10 RO/BFO object properties**.
Nine local data properties and the assertion container are application bookkeeping,
not biological classes or scientific relation candidates.

Scientific labels, definitions and synonym categories come exclusively from the
SHA-256-verified upstream RDF fragments in `manifest.json`. Missing definitions
remain absent. `application.ttl` does not annotate or redefine upstream terms and
contains no project-created scientific classes or effect properties.

Exact synonyms are lexical candidates, not context-free identity proof. Related,
broad and narrow synonyms never establish equivalence. Use only a concept/property
whose definition fits; retain unmatched predicates and entities as unresolved.
The selected vocabulary is a configurable potato-nutrition research boundary,
not a claim-specific set of acceptance rules. No remote imports are followed
while processing a document. A changed catalog invalidates older mappings.

The fragments include bounded ancestors and do not constitute full ontology
imports or establish complete OWL consistency. The manifest records URLs, hashes
and retrieval times; it does not claim a particular release when none was verified.
