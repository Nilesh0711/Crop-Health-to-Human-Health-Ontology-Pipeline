"""Experimental fixed-position extraction output; not the active run contract."""
from copy import deepcopy

from backend.providers.response_repair import validate_schema
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA

ROW_FIELDS = ('subject', 'relation', 'object', 'evidence_sentence_ids',
              'crop_or_species', 'population', 'condition', 'comparator', 'metric')


def response_schema(allowed_numbers):
    fields = EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']['properties']
    evidence = deepcopy(fields['evidence_sentence_ids'])
    evidence['items']['enum'] = list(allowed_numbers)
    return {'type': 'object', 'required': ['claims'], 'additionalProperties': False,
        'properties': {'claims': {'type': 'array', 'items': {
            'type': 'array', 'minItems': len(ROW_FIELDS), 'maxItems': len(ROW_FIELDS),
            'prefixItems': [deepcopy(fields[k]) for k in ROW_FIELDS[:3]] + [evidence]
                + [deepcopy(fields['qualifiers']['properties'][k]) for k in ROW_FIELDS[4:]],
            'items': False}}}}


def decode(response, allowed_numbers):
    """Validate before translating positions; never infer or repair missing cells."""
    validate_schema(response, response_schema(allowed_numbers))
    claims = []
    for row in response['claims']:
        values = dict(zip(ROW_FIELDS, row))
        claims.append({**{k: values[k] for k in ROW_FIELDS[:4]},
            'qualifiers': {k: values[k] for k in ROW_FIELDS[4:] if values[k] != ''}})
    return {'claims': claims}


def prompt(object_prompt):
    """Keep scientific instructions; replace only the wire-format instructions."""
    before, output = object_prompt.split('# OUTPUT: FIVE REQUIRED FIELDS', 1)
    # Runtime target and claim-budget instructions follow the output section.
    tail = output[output.index('\nTARGET/CONTEXT CONTRACT:'):] if '\nTARGET/CONTEXT CONTRACT:' in output else ''
    return before + '''# OUTPUT: FIXED-POSITION JSON ROWS
Return one JSON object with a claims array. Each claim is an array of exactly nine values:
1. subject: nonempty literal source phrase.
2. relation: complete source-faithful word or phrase.
3. object: nonempty literal source phrase.
4. evidence_sentence_ids: a nonempty array of unique ascending original target sentence numbers. Include every sentence needed for the relationship, antecedents and restrictions. Non-adjacent IDs are allowed; never renumber IDs.
5. crop_or_species: source-stated crop/species, or an empty string.
6. population: source-stated population/cultivar, or an empty string.
7. condition: source-stated conditions/restrictions, including organ/bearer, treatment, dose, duration and medium where applicable, or an empty string. Combine multiple details in this string.
8. comparator: source-stated comparator, or an empty string.
9. metric: source-stated measurement/result, preserving units, denominators, numbers and aggregation, or an empty string.
Do not infer potato from the project scope. Preserve all applicable qualifiers, without shortening restrictions. Do not add, omit or rearrange positions. Return no type hints, copied passages, claim text, explanations or extra cells. Empty optional values must still occupy their positions as empty strings. Code attaches literal passages, context, pages and citations. An empty claims array is valid.
''' + tail
