"""Five-column grounding records; parse delimiters without guessing or repair."""
from backend.providers.structured_output import SchemaValidationError
from backend.stages.ontology_grounding.contract import GROUNDING_INSTRUCTIONS, check_response

VERSION = 'grounding-tsv-v1'
ROLES = ('scope', 'subject', 'object', 'relation')
FORMAT = '''
OUTPUT: plain tab-separated records, exactly five columns per line:
record_id<TAB>kind<TAB>decision<TAB>target<TAB>reason
Use literal TAB characters between columns and a newline between records. No header, JSON,
Markdown, quotes around fields, blank lines, extra columns or explanatory text outside the records.
For EACH supplied claim ID, return exactly FOUR records:
- <claim_id>.scope : kind=scope; decision=in_scope/out_of_scope/needs_review; target=-.
- <claim_id>.subject : kind=concept/particular/state; decision=exact/broader/narrower/related/unresolved;
  target=one of that subject's supplied T identifiers, or - when unresolved.
- <claim_id>.object : same fields, using only that object's supplied T identifiers.
- <claim_id>.relation : kind=relation; decision=exact/unresolved;
  target=one supplied R identifier, or - when unresolved.
The dash - means no target. It is required exactly when the decision is unresolved, and for scope.
Reasons must be nonempty short prose on ONE line, at most 600 characters; no tabs or line breaks.
Every record has EXACTLY FOUR TAB delimiters and FIVE fields. Kind is mandatory even when unresolved.
Unresolved means no aligned target; it does not remove the independent concept/particular/state decision.
Do not echo the <TAB> marker; use actual tab delimiters. Preserve punctuation normally inside reasons.
IDs are supplied by the application; copy each required record ID exactly once. Stop after the last record.
Unresolved or needs_review are valid decisions; never force a mapping to complete an assertion.
'''
SYSTEM = GROUNDING_INSTRUCTIONS + FORMAT


def required_ids(payload):
    return [f'{key}.{role}' for key in payload['claims'] for role in ROLES]


def instructions(payload):
    templates = []
    for key in payload['claims']:
        templates.extend([
            '\t'.join([key + '.scope', 'scope', '<scope_decision>', '-', '<scope_reason>']),
            '\t'.join([key + '.subject', '<entity_kind>', '<mapping>', '<subject_target_or_dash>', '<reason>']),
            '\t'.join([key + '.object', '<entity_kind>', '<mapping>', '<object_target_or_dash>', '<reason>']),
            '\t'.join([key + '.relation', 'relation', '<relation_mapping>', '<relation_target_or_dash>', '<reason>']),
        ])
    return SYSTEM + ('\nRequired five-field record templates, one per line. Replace every angle-bracket placeholder '
                     'with your decision; preserve the fixed IDs, scope/relation kind markers and TAB delimiters. '
                     'Do not output the angle brackets or omit an unresolved entity kind.\n') + '\n'.join(templates)


def parse_rows(content, payload):
    """Construct the internal response only from complete, validated records.

    LF and CRLF delimiters are accepted. No trimming, delimiter guessing,
    escaping, partial-response selection or scientific correction is performed.
    """
    if not isinstance(content, str) or not content:
        raise SchemaValidationError('Grounding rows: empty answer')
    if any(ord(c) < 32 and c not in '\t\n\r' or ord(c) == 127 for c in content):
        raise SchemaValidationError('Grounding rows: invalid control character')
    lines = content.split('\n')
    if lines[-1] == '':
        lines.pop()
    expected = set(required_ids(payload))
    records = {}
    for number, line in enumerate(lines, 1):
        if line.endswith('\r'):
            line = line[:-1]  # CRLF is a delimiter, not part of a field value.
        if '\r' in line:
            raise SchemaValidationError(f'Grounding rows: embedded carriage return at line {number}')
        fields = line.split('\t')
        if len(fields) != 5:
            raise SchemaValidationError(f'Grounding rows: line {number} must contain exactly five tab-separated fields')
        identifier, kind, decision, target, reason = fields
        if identifier not in expected:
            raise SchemaValidationError(f'Grounding rows: unknown record ID at line {number}')
        if identifier in records:
            raise SchemaValidationError(f'Grounding rows: duplicate record ID {identifier}')
        if not reason.strip() or len(reason) > 600:
            raise SchemaValidationError(f'Grounding rows: {identifier} requires a nonempty reason of at most 600 characters')
        role = identifier.rsplit('.', 1)[1]
        if role == 'scope' and (kind != 'scope' or target != '-'):
            raise SchemaValidationError(f'Grounding rows: {identifier} requires kind scope and no target (-)')
        if role == 'relation' and kind != 'relation':
            raise SchemaValidationError(f'Grounding rows: {identifier} requires kind relation')
        records[identifier] = (kind, decision, target, reason)
    if records.keys() != expected:
        raise SchemaValidationError(f'Grounding rows: missing required record IDs {sorted(expected - records.keys())}')
    answer = {'results': []}
    for key in payload['claims']:
        scope = records[f'{key}.scope']
        row = {'claim_id': key, 'scope': scope[1], 'scope_reason': scope[3]}
        for role in ('subject', 'object', 'relation'):
            kind, decision, target, reason = records[f'{key}.{role}']
            row[role] = {'reason': reason, 'mapping': decision, 'term': None if target == '-' else target}
            if role != 'relation':
                row[role + '_denotation'] = kind
        answer['results'].append(row)
    check_response(answer, payload)
    return answer
