"""Assess source-bound measured comparisons independently of assertion predicates."""
import re

from backend.evaluation.comparison_pilot import assess_comparison_row, ground_trait_location_components


def assess_observation(row, inventory):
    claim = row['original_claim']
    metadata = claim['measured_comparison']
    qualifiers = claim.get('qualifiers', {})
    evidence = claim.get('source_evidence_span', '')
    binding = row['bindings']['object']
    trait = {'phrase': claim['object']}
    if binding['mapping'] in {'exact', 'broader'}:
        trait.update({k: binding.get(k) for k in ('iri', 'label', 'mapping')})
    components = binding.get('components')
    if inventory and not components:
        with inventory.connection() as db:
            components = ground_trait_location_components(claim['object'], trait, db, source_text=evidence,
                                                          definitions=row.get('abbreviation_definitions', []))
    if components:
        trait.update(component_status=components['status'],
                     grounding_completeness=components.get('grounding_completeness'),
                     ratio_components=components.get('ratio_components'),
                     grounded_components=components.get('grounded_components', []))
    observation = {**metadata, 'measured_trait': trait, 'study_group': claim['subject'],
                   'comparator_label': qualifiers.get('comparator') or '',
                   'condition': {'phrase': qualifiers.get('condition') or ''},
                   'reported_difference': qualifiers.get('metric') or claim['relation'],
                   'source_sentence': evidence}
    result = assess_comparison_row(observation, source_text=evidence)
    comparator = observation['comparator_label']
    if (re.search(r'\band\b', comparator, re.I) and re.search(r'\brespectively\b', evidence, re.I)
            and not re.search(r'\b(?:combined|pooled|joint)\b', comparator, re.I)):
        result['hold_reasons'].append('possible_pooled_pairwise_comparators')
        result['validation_issues'].append('Verify each named baseline against its own reported difference.')
        result.update(complete=False, complete_explicit_baseline=False, complete_unspecified_baseline=False, review='held')
    row['comparison_assessment'] = {**observation, **result}
    for code in result['hold_reasons']:
        if not any(w.get('code') == code for w in row.get('extraction_warnings', [])):
            row.setdefault('extraction_warnings', []).append({'code': code, 'message': code.replace('_', ' '), 'source_text': evidence})
