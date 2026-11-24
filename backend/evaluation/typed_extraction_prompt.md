Extract faithful, atomic, source-supported scientific findings matching the research scope below.

{research_scope}

Extraction rules:
- Extract one finding per claim record. Ontology identifiers are resolved later. Never output or invent ontology IDs.
- Copy endpoint wording and abbreviations verbatim as printed (for example, write `LP` or `P` if the sentence says `LP` or `P`; never expand `LP` into `low phosphorus (LP)` or insert missing words; preserve balanced parentheses).
- Quote every anchor (`subject_anchor.quote`, `relation_anchor.quote`, `object_anchor.quote`) as an exact, **contiguous** substring printed in `sentence_id`. Never omit intermediate words inside a quote or splice non-adjacent words; for a split outcome/comparator inside a coordination (e.g., `both X and Y`, `X and Y`, or `B and C, respectively`), quote the full contiguous coordinated span in the anchor while keeping `subject` or `object` as the single item. Never quote standalone table significance asterisks (`*`, `**`, `***`) as a `relation_anchor.quote`.
- Split **only** findings the source supports independently—whether in object position (`both X and Y`) or subject position (`X and Y correlated with Z`, `X and Y efficiency correlated with Z`)—into separate claims. Pair each claim with its own matching metric and comparator in textual order.
- Keep **genuine joint effects** (such as multi-factor statistical interactions `A × B`, `X, Y, and their interaction`, or joint co-treatments reported without separate single-factor effects), ratios, efficiencies, and compound traits intact—never split a joint interaction into separate single-factor claims unless independently stated.
- Preserve exact predicate strength (`affects` stays `affects`, never `improves`), direction, negation, uncertainty, and statistical non-significance; keep predicate phrases purely verbal/comparative by placing percentages and magnitudes in `qualifiers.metric` rather than inside `relation`.
- Put contextual conditions, cultivars/populations (`Cultivar A and Cultivar B`), comparison baselines, and measurements (including percentages such as `43.9%`) in `qualifiers`, never as a standalone endpoint in `subject` or `object` and without appending cultivar/population clauses onto an unrelated trait name.
- Keep active grammatical roles faithful: when the text says agent X performs or fails an action on target Y (e.g., `plants cannot acquire P`), put X (`plants`) in `subject` and Y (`P`) in `object`.
- In comparisons (`less than`, `higher than`), keep the measured trait or rate (e.g., `P uptake`) on the measured side rather than swapping the baseline group into `subject` and the trait into `object`.
- Every claim MUST cite a `target` sentence in `relation_anchor.sentence_id`. `neighbor` sentences may supply antecedents or qualifiers only, never standalone findings.
- When the `target` sentences report no eligible finding within scope, return `{"claims": []}`.

Field template (generated from the response schema):
{field_template}

Examples:

Example 1 (splitting coordinated outcomes with paired measurements):
Input:
1 target: Treatment T increased Trait X by 12% and Trait Y by 8% compared with Control C.
Output:
{
  "claims": [
    {
      "subject": "Treatment T",
      "subject_type": "condition or exposure",
      "relation": "increased",
      "object": "Trait X",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": null, "condition": null, "comparator": "Control C", "metric": "12%"},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 1, "quote": "Treatment T"},
      "relation_anchor": {"sentence_id": 1, "quote": "increased"},
      "object_anchor": {"sentence_id": 1, "quote": "Trait X"}
    },
    {
      "subject": "Treatment T",
      "subject_type": "condition or exposure",
      "relation": "increased",
      "object": "Trait Y",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": null, "condition": null, "comparator": "Control C", "metric": "8%"},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 1, "quote": "Treatment T"},
      "relation_anchor": {"sentence_id": 1, "quote": "increased"},
      "object_anchor": {"sentence_id": 1, "quote": "Trait Y"}
    }
  ]
}

Example 2 (preserving a non-significant comparison):
Input:
1 target: Factor A did not significantly alter Ratio Z relative to Control B.
Output:
{
  "claims": [
    {
      "subject": "Factor A",
      "subject_type": "condition or exposure",
      "relation": "did not significantly alter",
      "object": "Ratio Z",
      "object_type": "trait or measurement",
      "assertion_status": "non_significant",
      "qualifiers": {"crop_or_species": null, "population": null, "condition": null, "comparator": "Control B", "metric": null},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 1, "quote": "Factor A"},
      "relation_anchor": {"sentence_id": 1, "quote": "did not significantly alter"},
      "object_anchor": {"sentence_id": 1, "quote": "Ratio Z"}
    }
  ]
}

Example 3 (paired comparative values + coordinated subjects with contiguous anchors vs. genuine joint interaction):
Input:
1 target: Trait M of Group S was 14% and 21% lower than that of Group I and Group L, respectively.
2 target: Specific Trait Q and Efficiency R correlated with Total Trait Q in Population V.
3 target: Factor A × Factor B interaction significantly affected Trait N.
Output:
{
  "claims": [
    {
      "subject": "Trait M of Group S",
      "subject_type": "trait or measurement",
      "relation": "was lower than",
      "object": "that of Group I",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": "Group S", "condition": null, "comparator": "Group I", "metric": "14%"},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 1, "quote": "Trait M of Group S"},
      "relation_anchor": {"sentence_id": 1, "quote": "was 14% and 21% lower than"},
      "object_anchor": {"sentence_id": 1, "quote": "that of Group I"}
    },
    {
      "subject": "Trait M of Group S",
      "subject_type": "trait or measurement",
      "relation": "was lower than",
      "object": "that of Group L",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": "Group S", "condition": null, "comparator": "Group L", "metric": "21%"},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 1, "quote": "Trait M of Group S"},
      "relation_anchor": {"sentence_id": 1, "quote": "was 14% and 21% lower than"},
      "object_anchor": {"sentence_id": 1, "quote": "that of Group I and Group L"}
    },
    {
      "subject": "Specific Trait Q",
      "subject_type": "trait or measurement",
      "relation": "correlated with",
      "object": "Total Trait Q",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": "Population V", "condition": null, "comparator": null, "metric": null},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 2, "quote": "Specific Trait Q and Efficiency R"},
      "relation_anchor": {"sentence_id": 2, "quote": "correlated with"},
      "object_anchor": {"sentence_id": 2, "quote": "Total Trait Q"}
    },
    {
      "subject": "Efficiency R",
      "subject_type": "trait or measurement",
      "relation": "correlated with",
      "object": "Total Trait Q",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": "Population V", "condition": null, "comparator": null, "metric": null},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 2, "quote": "Specific Trait Q and Efficiency R"},
      "relation_anchor": {"sentence_id": 2, "quote": "correlated with"},
      "object_anchor": {"sentence_id": 2, "quote": "Total Trait Q"}
    },
    {
      "subject": "Factor A × Factor B interaction",
      "subject_type": "condition or exposure",
      "relation": "significantly affected",
      "object": "Trait N",
      "object_type": "trait or measurement",
      "assertion_status": "affirmed",
      "qualifiers": {"crop_or_species": null, "population": null, "condition": null, "comparator": null, "metric": null},
      "support_sentence_ids": [],
      "subject_anchor": {"sentence_id": 3, "quote": "Factor A × Factor B interaction"},
      "relation_anchor": {"sentence_id": 3, "quote": "significantly affected"},
      "object_anchor": {"sentence_id": 3, "quote": "Trait N"}
    }
  ]
}
