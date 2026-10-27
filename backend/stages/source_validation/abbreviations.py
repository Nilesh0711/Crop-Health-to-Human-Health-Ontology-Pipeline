"""Apply only the source model's explicitly grounded name expansions."""
from copy import deepcopy
import re

from backend.domain.alignment import exact_in
from backend.domain.vocabulary import normalize_entity


SOURCE_VALIDATION_POLICY = 'source-context-grounded-v2'
TEXT_FIELDS = ('subject', 'object', 'relation', 'claim_text', 'source_context',
               'qualifiers.crop_or_species', 'qualifiers.population', 'qualifiers.condition',
               'qualifiers.comparator', 'qualifiers.metric')
ABBREVIATION_SCHEMA = {
    'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['clear', 'expanded', 'unresolved']},
        'unresolved': {'type': 'array', 'items': {'type': 'string'}},
        'corrections': {'type': 'array', 'maxItems': 12, 'items': {
            'type': 'object', 'properties': {
                'field': {'type': 'string', 'enum': list(TEXT_FIELDS)},
                'before': {'type': 'string'}, 'after': {'type': 'string'},
                'short_form': {'type': 'string'}, 'full_form': {'type': 'string'},
                'definition_quote': {'type': 'string'}, 'rationale': {'type': 'string'}},
            'required': ['field', 'before', 'after', 'short_form', 'full_form', 'definition_quote', 'rationale'],
            'additionalProperties': False}}},
    'required': ['status', 'unresolved', 'corrections'], 'additionalProperties': False,
}
ABBREVIATION_INSTRUCTIONS = (
    'Before judging fidelity, inspect ALL canonical claim fields, claim_text, source_occurrence_context and qualifiers '
    'for abbreviations, acronyms and abbreviated organism genera. Use full names in these fields, without adding '
    'the short form in parentheses. Preserve exact PDF quotations, source surface mentions, sentence IDs, units, '
    'chemical formulae and formal gene/strain identifiers. Do not guess an expansion from world knowledge. '
    'Return abbreviation_review: status=clear if none need expansion, expanded if every abbreviation is '
    'unambiguously expanded from supplied source context, or unresolved if any identity is undefined/ambiguous. '
    'For each field changed, provide before and after, short_form, full_form, a verbatim definition_quote '
    'from the supplied context containing BOTH forms, and a brief rationale for their identity. Each correction '
    'may ONLY replace that short form with its full form in that field; repeat corrections in order for multiple '
    'short forms in one field. Already spelled-out canonical names need NO correction even if the PDF uses '
    'an abbreviation. Never propose before=after; do not expand abbreviations that occur only in verbatim proof. '
    'Correct the claim_text and qualifier occurrences too. For relation identifiers '
    'use a spelled-out snake_case full_form. Make NO other edits to predicates, conditions, modality or types. '
    'Judge the claim AFTER those proposed name expansions in this SAME response: canonical values in '
    'entity_alignment must equal the expanded endpoints, and all fidelity booleans/proof must describe that '
    'expanded claim. Expanding a name does not establish its scientific correctness. '
    'If any abbreviation cannot be resolved from the supplied text, list it in unresolved and choose needs_review. '
    'Abstract example (placeholder names, never evidence): source "Disease D (DD) affects Crop C" '
    'and subject="DD": propose subject="Disease D", keep proof.subject="DD", quote "Disease D (DD)" '
    'for the expansion, and also expand DD wherever it occurs in claim_text or qualifiers. '
)


def _value(candidate, field):
    if field.startswith('qualifiers.'):
        return (candidate.get('qualifiers') or {}).get(field.split('.', 1)[1], '')
    return candidate.get(field, '')


def _set(candidate, field, value):
    if field.startswith('qualifiers.'):
        candidate.setdefault('qualifiers', {})[field.split('.', 1)[1]] = value
    else:
        candidate[field] = value


def prepare_name_expansions(candidate, review, context):
    """Verify edit integrity and quoted provenance; the model decides identity.

    There is no abbreviation dictionary, acronym inference or local correction.
    A whole proposal is held if any edit changes more than its declared name.
    """
    working = deepcopy(candidate)
    if not isinstance(review, dict):
        # Test/replayed old response objects may predate the required schema.
        # Live provider responses must satisfy the schema before reaching here.
        return working, []
    errors = []
    corrections = review.get('corrections') or []
    if review.get('status') == 'unresolved' or review.get('unresolved'):
        errors.append('An abbreviation has no unambiguous source-defined expansion')
    for edit in corrections:
        if not isinstance(edit, dict) or edit.get('field') not in TEXT_FIELDS:
            errors.append('Abbreviation correction targets an unknown claim field')
            continue
        field, before, after = (edit.get(k) for k in ('field', 'before', 'after'))
        short, full, quote = (edit.get(k) for k in ('short_form', 'full_form', 'definition_quote'))
        if (isinstance(before, str) and before == after == _value(working, field)
                and isinstance(short, str) and short
                and not re.search(r'(?<!\w)' + re.escape(short) + r'(?!\w)', before)):
            # An annotation on an already expanded field changes nothing.
            # Do not invent a definition or present it as an applied edit.
            continue
        if (isinstance(after, str) and after == _value(working, field)
                and all(isinstance(v, str) and v for v in (before, short, full, quote))
                and exact_in(quote, context) and exact_in(short, quote) and exact_in(full, quote)
                and re.sub(r'(?<!\w)' + re.escape(short) + r'(?!\w)', lambda _: full, before) == after):
            continue  # Already expanded; do not apply or record a fictitious edit.
        if (not all(isinstance(v, str) and v.strip() for v in (before, after, short, full, quote))
                or not str(edit.get('rationale') or '').strip() or before != _value(working, field)
                or short == full or not exact_in(quote, context)
                or not exact_in(short, quote) or not exact_in(full.replace('_', ' ') if field == 'relation' else full, quote)):
            errors.append(f'{field} expansion lacks matching original text or a source definition')
            continue
        expected, count = re.subn(r'(?<!\w)' + re.escape(short) + r'(?!\w)', lambda _: full, before)
        if not count or expected != after:
            errors.append(f'{field} expansion changes content beyond the declared abbreviation')
            continue
        _set(working, field, after)
    if errors:
        return deepcopy(candidate), errors
    return working, []


def commit_name_expansions(original, corrected, review):
    edits = [edit for edit in (review.get('corrections') or [])
             if edit.get('before') != edit.get('after') and _value(original, edit['field']) != _value(corrected, edit['field'])] if isinstance(review, dict) else []
    if not edits:
        return []
    for field in {edit['field'] for edit in edits}:
        _set(original, field, _value(corrected, field))
    for role in ('subject', 'object'):
        if any(edit['field'] == role for edit in edits) and original.get(f'{role}_type'):
            original[f'{role}_normalization'] = normalize_entity(original[role], original[f'{role}_type'])
    original.setdefault('source_name_corrections', []).extend(deepcopy(edits))
    return deepcopy(edits)
