# First entity-grounding pilot

Focus: potato, soil nutrients and crop nutrition. Start with meaning
and identity, not end-to-end pathway truth. No source/external validation is enabled.

`potato_nutrition_review.jsonl` freezes **20 endpoint occurrences** from the completed
Kirchgesser extraction against the restored potato profile version 3. Cases are
candidate-stratified to include different expressions and vocabulary gaps.
This is a deliberately small diagnostic set, not a random or representative
accuracy benchmark. `potato_review_sheet.md` is the readable index;
the JSONL retains the full original evidence and context.

The older `root_length_review.jsonl` and `review_sheet.md` are frozen profile-2
comparison material. Their labels are not transferred to the changed reference;
the runner deliberately refuses to run them against profile 3.

All gold fields start empty and all predictions start `null`. Suggested reference
candidates are lexical retrieval aids, not answers. Existing broader-scope model
decisions have not been copied into gold labels.

## Review before testing

For each occurrence, read the exact source and local context. Fill `gold`:

- `ontology_iri`: a defensible reference concept, or `null` for no mapping in this module.
- `mapping`: exact, broader, narrower, related or unresolved. Direction is from the
  source meaning to the chosen concept: broader means the selected concept is broader.
- `denotation`: concept, particular or state. A shared type never identifies two samples.
- `identity_group`: the same group only for genuinely identical generic meanings;
  distinct observations/states keep different groups. Relatedness is not identity.
- `scope`: in_scope, out_of_scope or needs_review under the narrow question.
- `reviewer`, `notes`, then `reviewed: true`. Record uncertainty instead of guessing.

Check a whole-root-system sum versus the length of one root, an anatomical root
versus its length/biomass, and a chemical versus its deficiency condition. A low
supply treatment is not automatically a diagnosed biological deficiency. Do not
broaden an established definition merely to improve the mapping count.

## Isolated Thinking comparison (manual, incurs model usage)

These commands ground the source claims containing the selected occurrences.
Both endpoints are assessed to preserve the original claim context, but only the
20 selected occurrences enter evaluation. Reviewer labels are never sent to the
model. Extraction, scientific validation and production result files are untouched.
Choose new output directories for repeat experiments; results are never overwritten.

```powershell
.venv\Scripts\python.exe -m backend.cli.ontology ground-pilot --sample analysis/grounding_pilot/potato_nutrition_review.jsonl --output analysis/grounding_pilot/potato_results_off --thinking off
.venv\Scripts\python.exe -m backend.cli.ontology evaluate --labels analysis/grounding_pilot/potato_nutrition_review.jsonl --current-predictions --results-root analysis/grounding_pilot/potato_results_off

.venv\Scripts\python.exe -m backend.cli.ontology ground-pilot --sample analysis/grounding_pilot/potato_nutrition_review.jsonl --output analysis/grounding_pilot/potato_results_on --thinking on
.venv\Scripts\python.exe -m backend.cli.ontology evaluate --labels analysis/grounding_pilot/potato_nutrition_review.jsonl --current-predictions --results-root analysis/grounding_pilot/potato_results_on
```

Use identical reviewed labels, sources and reference for both. Compare alignment
and denotation accuracy, incorrect merges, missed equivalents, unresolved results,
retry count, tokens and time. Request logs and raw responses remain beside each
pilot result. No claim that Thinking improves performance is made before testing.
Changed source/reference signatures prevent label reuse. Without reviewed labels,
accuracy remains unavailable. Changes in source/classification must be reviewed,
not repaired deterministically to match a desired answer.

## Testing the rewritten extraction prompt

The original 63-claim run is preserved as a baseline. In the frontend, a fresh
**New extraction and grounding** run tests the rewritten potato extraction prompt. **Ground
saved claims** instead re-evaluates the older extraction. Old mappings
are stale after the profile change; this is expected. Source evidence is preserved.
Review results in **Entity registry**, with vocabulary gaps available separately.

To prepare a new unlabelled sample without model calls:

```powershell
.venv\Scripts\python.exe -m backend.cli.ontology prepare-pilot --document Kirchgesser_2023_Potato_Root_Architecture_Phosphorus --limit 20 --output analysis/grounding_pilot/new_review.jsonl
```

Automatic sampling is candidate-stratified, not a relevance judgement; inspect
the case mix before freezing a new set. Never overwrite an existing review set.

## Reference basis

External definitions and identifiers are unchanged frozen Ontobee fragments. The
profile records the selected classes; local definitions remain explicitly labelled.
Ontobee serves ontology terms; it does not automatically resolve paper mentions.
See [Ontobee](https://ontobee.org/introduction) and the [OBO reuse principles](https://obofoundry.org/principles/fp-001-open.html).
