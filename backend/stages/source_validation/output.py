"""Avoid regenerating input values or duplicated literal anchors in source output."""
from copy import deepcopy

from backend.stages.source_validation.abbreviations import TEXT_FIELDS


def compact_source_schema(schema):
    schema = deepcopy(schema)
    properties = schema['properties']
    schema['required'].remove('occurrence_provenance_fields')
    properties['proof']['required'] = ['relation']
    for role in ('subject', 'object'):
        alignment = deepcopy(properties['entity_alignment']['properties'][role])
        properties['entity_alignment']['properties'][role] = alignment
        alignment['required'] = [field for field in alignment['required'] if field != 'canonical']
        alignment['required'].remove('evidence')
        alignment['allOf'] = [{
            'if': {'properties': {'kind': {'enum': ['same_concept', 'coordination']}}},
            'else': {'required': ['evidence']},
        }]
        alignment['properties']['evidence']['description'] = (
            'For same_concept omit only when the mention alone is the full anchor; '
            'code reuses that literal mention. Other kinds require explicit binding evidence.')
        alignment['properties']['canonical']['description'] = 'Copied from the assessed claim by code; omit from model output.'
        properties['proof']['properties'][role]['description'] = 'Copied from the corresponding alignment mention by code; omit from model output.'
    correction = properties['abbreviation_review']['properties']['corrections']['items']
    correction['required'].remove('before')
    correction['properties']['before']['description'] = 'Copied from the current input field by code; omit from model output.'
    review = properties['abbreviation_review']
    review['required'] = ['status']
    review['allOf'] = [
        {'if': {'properties': {'status': {'const': 'expanded'}}},
         'then': {'required': ['corrections'], 'properties': {'corrections': {'minItems': 1}}}},
        {'if': {'properties': {'status': {'const': 'unresolved'}}},
         'then': {'required': ['unresolved'], 'properties': {'unresolved': {'minItems': 1}}}},
    ]
    return schema


def bind_source_output(candidate, response):
    """Copy known inputs/selections only; never fix supplied values or infer meaning.

    Explicitly returned values remain untouched and pass the existing checks.
    Canonical labels bind later, after validating model-proposed name expansions.
    """
    response = deepcopy(response)
    response.setdefault('occurrence_provenance_fields', [])
    review = response.get('abbreviation_review')
    if isinstance(review, dict):
        review.setdefault('corrections', [])
        review.setdefault('unresolved', [])
    alignments = response.get('entity_alignment') or {}
    for mapping in alignments.values():
        if isinstance(mapping, dict) and isinstance(mapping.get('mention'), str):
            if mapping.get('kind') in {'same_concept', 'coordination'} or not mapping.get('evidence'):
                mapping.setdefault('evidence', mapping['mention'])
    proof = response.get('proof')
    if isinstance(proof, dict):
        for role in ('subject', 'object'):
            mention = (alignments.get(role) or {}).get('mention')
            if role not in proof and isinstance(mention, str):
                proof[role] = mention
    values = {field: (candidate.get('qualifiers') or {}).get(field.split('.', 1)[1], '')
              if field.startswith('qualifiers.') else candidate.get(field, '') for field in TEXT_FIELDS}
    for edit in (response.get('abbreviation_review') or {}).get('corrections', []):
        field = edit.get('field')
        if field in values:
            edit.setdefault('before', values[field])
            if isinstance(edit.get('after'), str):
                values[field] = edit['after']
    return response
