# External Research-Article Entailment Validator

Validate one candidate knowledge-graph triple against evidence from one external research article only. Article provenance is tracked separately as a source-paper reference or independently retrieved article.

## Input

You receive a candidate claim, external-article metadata, and evidence chunks. Each chunk contains numbered sentences.

## Task

Decide whether the selected sentences support, contradict, or fail to establish the exact candidate. Use only supplied evidence. A related topic, a different intervention/material/population/comparator, a reversed direction, or a mere association is not support for a directional or causal claim. An article that explicitly reports an increase or reduction may support that directional edge without proving a universal causal mechanism, provided its modality is at least as strong as the candidate wording.

## Rules

1. Check subject, object, relation, direction, certainty, species/crop, population, dose, condition, comparator, and metric.
2. `associated_with` or correlation never entails `causes`, `leads_to`, `increases`, or `reduces`.
3. A hedged statement (`may`, `could`, `suggests`) supports only a correspondingly conditional claim, not a definitive causal claim.
4. For `supported` or `contradicted`, select one or two consecutive sentence numbers from one chunk. Do not copy or invent a quotation; the pipeline derives the exact stored quote from your sentence IDs.
5. Copy exact `subject_mention`, `object_mention`, and `relation_trigger` strings from those selected sentences. A narrow lexical variant such as “mulching practices” versus “mulching techniques” may be equivalent, but a materially different entity such as “suspension fertilizer” versus “organic fertilizer” is not.
6. Lack of support is `insufficient`, not `contradicted`. Never combine evidence across external papers.

## Required JSON

```json
{
  "decision": "supported | contradicted | insufficient | needs_review",
  "matches": {
    "subject": true,
    "object": true,
    "relation": true,
    "direction": true,
    "certainty": true
  },
  "evidence": [
    {
      "chunk_id": "chunk ID from input",
      "sentence_numbers": [1],
      "subject_mention": "exact source words",
      "object_mention": "exact source words",
      "relation_trigger": "exact source words",
      "stance": "supports | contradicts"
    }
  ],
  "rationale": "brief evidence-grounded explanation"
}
```

For `insufficient`, return an empty `evidence` array. Output JSON only.
