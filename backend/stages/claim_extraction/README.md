# Direct extraction modes

New document runs default to **Target / context**. Run settings also offer
**Whole packet**, the previous extraction algorithm with its priority evidence
index. Both use the assigned primary model and the existing schema, exact PDF
anchors, duplicate handling and downstream validation. Relations are open:
common names are suggestions, not a whitelist. JSON/schema validation checks
structure and provisional label names, not biological type compatibility.
`Unresolved` keeps uncertain classifications for evidence assessment.
`PlantPathogen` and `PathogenTrait` distinguish organisms/strains and their
measured characteristics from crop diseases and crop traits.

Heuristic concerns about generic entity labels, comparisons, modality, captions
or association are stored as `extraction_review_hints`, not rejections. The
extractor no longer rewrites disease/container identities, removes restrictions
or changes certainty locally. Source fidelity remains a model decision; the
joint external model assesses type correctness and project scope with research
evidence. Original source anchors, sentence IDs and target ownership still gate
admission. Scientific assessment can retain a claim for review or exclude it.

Runtime examples use abstract Crop C / Disease D / Compound T entities. Real
paper claims belong in test fixtures and audit data, not reusable instructions.
There is no list of previously seen endpoint phrases or editorial sentences
that marks claims for special scope review or suppresses their priority index.
Whole-packet attention retains general pathway classes and relation cues;
target/context still assigns every parsed source sentence independently of
keywords. Scope and scientific meaning remain evidence-model decisions.

In target mode, `targeting.py` divides each original reading packet into smaller
regions of about 800 words. Paragraphs stay together when they fit; oversized
paragraphs split at sentence boundaries. Targets stay within chapters; nearby
sections may share a target while retaining each sentence's heading. Each
distinct parsed sentence occurrence belongs to one target,
including sentences with no domain keywords. Overlapping reading packets do
not cause the same sentence to be assigned twice. Targets cannot repair text
or paragraph boundaries already lost by the PDF parser.

One model call receives a marked `extraction_target` and a separate
`reading_context_only` block containing the rest of the original packet.
Original sentence numbers, page numbers and headings are retained. The model
may use the context to interpret referents, crop identity and restrictions;
all direct evidence sentence IDs must be unique, ascending and inside the target
(1?3 IDs). Non-adjacent fragments retain their original pages and IDs, explicit
gap markers and every intervening sentence for source validation. They are not
represented as one continuous source quotation.
There is no preliminary LLM relevance/discovery pass. Context-only findings
are rejected, including after response regeneration or syntax repair.

The default is **all eligible claims**, with no numeric claim-count cap. The
optional run limit means **maximum claims per extraction call**: per
target in target mode or per reading packet in whole-packet mode. Target mode
can therefore produce more calls and more candidates. An empty eligible-claim
array is valid. All targets being processed does not prove exhaustive claim
recall; the ledger records when the claim cap is reached.

## Runtime configuration

Frontend: **Run details → Run settings → Claim extraction**.

| API run option | Default | Allowed values |
| --- | --- | --- |
| `extraction_mode` | `target_context` | `target_context`, `whole_packet` |
| `extraction_target_words` | 800 | 200–2,000 |
| `extraction_generation_budget` | 24,576 | 8,192–32,768 |
| `claims_per_packet` | 0 | 0 = all eligible claims; 1–8 = optional maximum per call |

Backend environment defaults are `EXTRACTION_MODE`, `EXTRACTION_TARGET_WORDS`
and `EXTRACTION_MIN_OUTPUT_BUDGET`. The generation allowance is shared by
reasoning and final answer tokens; it is not an answer reservation or a promise
of provider context capacity. Thinking defaults to Off and can be enabled in
the assigned model's runtime settings.
The default initial ceiling rises from 12,288 to 24,576. Existing single-action
structured-response recovery can raise it to the configured regeneration cap
(32,768 by default). There are no model substitutions or unbounded retries.
Uncapped extraction requests a larger final-answer allowance based on target
size; it removes the JSON array `maxItems` limit. Physical generation limits
still apply, and truncation fails visibly after bounded recovery. No claim of
exhaustive scientific recall follows from removing the cap.

Each result retains original sentence numbers, exact source passage, surface
mentions, source page and heading, surrounding context, qualifiers and a short
claim restatement. The restatement is not substituted for source evidence.
Connected direct edges and explicit local references are extracted separately;
the [Pathway Assembly stage](../pathway_assembly/README.md) aligns shared entities
globally after external validation.

## Persistence and compatibility

- The activity log identifies target progress, original packet, input-token
  estimate, extraction words and generation usage. Provider errors remain visible.
- `extraction_packets.jsonl` stores target IDs, assigned original sentence IDs,
  completion/failed/not-started status and claim-limit flags. It is also saved
  when extraction stops on a primary-provider error.
- `extraction_contexts.jsonl` preserves original reading packets for claim
  repair. Candidate provenance retains the original packet/evidence numbers
  plus its target ID. Claim repair remains one explicitly targeted claim action.
- Checkpoints and summaries record the mode and target size. Resume preserves
  that policy and rejects explicit changes; use a new run to change it. Runs
  predating these fields are treated as whole-packet extractions. They are not
  relabelled as target/context work.
- Failed extraction is not resumable as a completed extraction. The v36 policy
  requires a fresh run for older checkpoints, since previously omitted claims
  cannot be restored by resuming validation alone. Historical artifacts remain.

Offline tests cover sentence ownership, context preservation, PDF anchors and
conditions, out-of-target rejection, bounded same-model regeneration, whole-
packet compatibility, persisted failure coverage, runtime API options and
historical resume behavior. These are contract checks, not evidence of improved
scientific accuracy; compare real-document results before drawing that conclusion.
