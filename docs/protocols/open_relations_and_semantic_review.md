# Open relation extraction and evidence-based semantic review

Implemented 2026-10-25. Pipeline contract: `external-validation-v35`.

## What was wrong with the stripe-rust rejection?

The Crop Health PDF states on parsed page 6 that newly spread stripe-rust
strains are more aggressive and adapted to higher temperatures. It cites
Milus et al. (2009) and Sørensen et al. (2014).

The failed response represented this as `stripe rust / has_trait / more
aggressive strains adapted to higher temperatures`, with `PlantDisease` and
`PlantTrait` labels. A JSON Schema conditional allowed `has_trait` subjects
only when labelled `Crop` or `FoodProduct`. Both response attempts therefore
failed despite containing valid JSON. This was an ontology modelling
restriction, not an evidence-based finding that the science was false.

The phenomenon has primary experimental support. Milus et al. compared
pre-2000 and post-2000 pathogen isolates on susceptible adult wheat under two
temperature regimes. The newer isolates were more aggressive and showed
adaptation to the warm regime. That supports qualified pathogen-strain claims,
not a universal property of every stripe-rust population.
[Milus et al., Phytopathology 99:89–94](https://apsjournals.apsnet.org/doi/10.1094/PHYTO-99-1-0089).

The qualification matters: a study of Australasian pathotypes found increased
aggressiveness without evidence of high-temperature adaptation in the tested
post-2002 population. Population, cultivar, experimental regime and measured
trait must survive extraction and comparison.
[Loladze et al., Plant Pathology 63:572–580](https://bsppjournals.onlinelibrary.wiley.com/doi/10.1111/ppa.12132).

The automatic schema rejection was too broad, but the exact triplet still
needs review. It mixes a disease condition, pathogen strains and compound
traits. The first response used a spread predicate as its `has_trait` trigger.
Those are source-fidelity/semantic questions, not JSON validity errors.
Separate strain-to-trait and strain-to-temperature-adaptation edges may be
more faithful when grounded in the selected source sentences. No local rule
automatically rewrites the historical triplet or marks it scientifically true.

## New boundary

1. **Strict local integrity:** valid JSON, required fields and data types,
   nonempty relation, provisional label names, assigned consecutive sentence
   IDs, exact source anchors, paper/chunk identity and quotation provenance.
   Existing evidence-binding checks still prevent substituting another entity
   or using another paper's text as proof.
2. **Open scientific predicates:** no extraction relation enum or type-pair
   conditional. The prompt suggests familiar names but accepts precise new
   predicates such as `produces`, `colonizes` or `adapts_to`. It also permits
   explicit causal, associative and comparative edges within project scope.
3. **Provisional types:** `PlantPathogen` and `PathogenTrait` distinguish strains
   and their characteristics from diseases and crop traits. `Unresolved`
   prevents forcing an uncertain entity into an incorrect label. Labels do not
   establish scientific correctness; unresolved or disputed labels need review.
4. **No heuristic semantic correction:** extraction no longer replaces a
   disease with grain or a section topic, strips conditions, rejects generic
   endpoints or rewrites modality. Heuristics become visible attention notes.
   Deduplication preserves different restrictions and certainty labels.
5. **Source fidelity:** the source model checks whether the original PDF
   actually states this edge, its referents, conditions and modality. Local
   lexical rules cannot override its decision or promote `needs_review`.
6. **External scientific assessment:** Pro receives the fixed claim and
   retrieved research to assess support/conflict, contextual types and domain
   scope. Lexical relation cues are attention notes, not scientific vetoes.
   Contradictions require exact, document-bound evidence plus consistent
   model-declared endpoint matches and relation/direction incompatibility;
   the predicate is not classified by a verb whitelist.
7. **Domain protection:** extraction instructions retain the project policy.
   The external model assesses scope even for a novel relation. An explicit
   `in_scope=false` result cannot enter the validated graph or global pathway
   assembly. It remains inspectable in document candidates and source/audit
   views. Pending scope is not inferred from keywords. Insufficient evidence
   does not establish truth or falsity.

## Verification and rollout

Offline tests exercise these boundaries and preserve required provenance
failures. The two saved failed response bodies also pass the revised schema
without edits. This demonstrates removal of the overrestrictive gate, not
better scientific accuracy or full validation of their contents. The diagnostic
replay does not change the saved run's accepted/rejected status.

Use a fresh document run after restarting the backend with this code. Older
checkpoints used an extraction policy that omitted some relations and cannot
recover them by resuming validation alone. The v35 version invalidates those
checkpoints and previous verdict caches rather than mixing policies silently.
Model choices, runtime Thinking controls, bounded response recovery, Stop,
claim repair, retrieval budgets and evidence provenance remain available.

A real-document comparison is still needed to measure recall, false support,
review load, latency and cost. No paid provider run was started by this change.
