"""Document-bound provenance checks; scientific equivalence belongs to the judge."""
from copy import deepcopy

from backend.domain.alignment import exact_in, normalized_phrase
from backend.stages.external_validation.proofs import _DECISION_SCHEMA, _sentences


SELECTION_SCHEMA = {'type': 'object', 'properties': {
    'chunk_id': {'type': 'string'},
    'sentence_numbers': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'minItems': 1, 'maxItems': 6}},
    'required': ['chunk_id', 'sentence_numbers'], 'additionalProperties': False}
PAPER_DECISION_SCHEMA = deepcopy(_DECISION_SCHEMA)
PAPER_DECISION_SCHEMA['properties']['evidence']['items']['properties']['sentence_numbers'].update(
    description='Exact sentence IDs, not range endpoints. [4,6] selects only 4 and 6; [4,6,8] selects only 4, 6 and 8. Use [4,5,6] to select every sentence from 4 through 6.',
    uniqueItems=True)
PAPER_DECISION_SCHEMA['properties']['context_selections'] = {'type': 'array', 'items': SELECTION_SCHEMA, 'maxItems': 8}
PAPER_DECISION_SCHEMA['allOf'] = [{
    'if': {'properties': {'decision': {'enum': ['supported', 'contradicted']}}, 'required': ['decision']},
    'then': {'required': ['context_selections']},
}]


def selected_context(document, selection, *, finding=False):
    """Reconstruct only real local IDs, keeping context gaps explicit."""
    chunk = next((c for c in document.get('evidence_chunks', []) if c.get('chunk_id') == selection.get('chunk_id')), None)
    numbers = selection.get('sentence_numbers')
    sentences = _sentences((chunk or {}).get('text'))
    maximum = 3 if finding else 6
    if (not chunk or not isinstance(numbers, list) or not 1 <= len(numbers) <= maximum
            or any(type(n) is not int for n in numbers) or numbers != sorted(set(numbers))
            or not 1 <= numbers[0] <= numbers[-1] <= len(sentences)):
        return None, 'Selection must use unique ascending sentence IDs from this paper/chunk'
    fragments, group, previous = [], [], None
    for number in numbers:
        if previous is not None and number != previous + 1:
            fragments.append(' '.join(group)); group = []
        group.append(sentences[number - 1]); previous = number
    fragments.append(' '.join(group))
    if finding and sum(len(fragment.split()) for fragment in fragments) > 180:
        return None, 'A finding quotation exceeds 180 words; choose a shorter finding and separate context'
    gap = ' [NON-ADJACENT EVIDENCE] ' if finding else ' [NON-ADJACENT CONTEXT] '
    return {**selection, **{key: chunk[key] for key in ('source_kind', 'table_id', 'row_number', 'figure_id') if key in chunk},
            'fragments': fragments, 'is_contiguous': len(fragments) == 1,
            'intervening_context': [{'number': number, 'text': sentences[number - 1]}
                                    for number in range(numbers[0], numbers[-1] + 1) if number not in numbers] if finding else [],
            'quoted_span': gap.join(fragments)}, None


def provenance(candidate, document, row):
    """Check identities/quotes, never infer a synonym or scientific role locally."""
    result, errors, finding_texts, context_texts = deepcopy(row), [], [], []
    result.pop('alignment_context_roles', None)
    result['candidate_id'] = candidate['candidate_id']
    result['evidence_document_id'] = document['evidence_document_id']
    for item in result.get('evidence') or []:
        selected, error = selected_context(document, item, finding=True)
        if error:
            errors.append(error); continue
        if item.get('quoted_span') is not None and item['quoted_span'] != selected['quoted_span']:
            errors.append('Stored quotation differs from its actual paper/sentence selection')
        item.update(selected)
        finding_texts.extend(selected['fragments'])
        context_texts.extend(sentence['text'] for sentence in selected['intervening_context'])
        for field in ('subject_mention', 'object_mention', 'relation_trigger'):
            if not any(exact_in(item.get(field), fragment) for fragment in selected['fragments']):
                errors.append(f'{field} must be literal wording in its own selected finding')
    contexts, binding_texts = [], []
    for selection in result.get('context_selections') or []:
        selected, error = selected_context(document, selection)
        if error:
            errors.append(error); continue
        contexts.append(selected); context_texts.extend(selected['fragments'])
        binding_texts.extend(selected['fragments'])
    result['binding_context'] = contexts
    if result.get('decision') in {'supported', 'contradicted'}:
        if len(result.get('evidence') or []) != 1:
            errors.append('A decisive paper judgement needs exactly one finding quotation')
        for role in ('subject', 'object'):
            mapping = (result.get('entity_alignment') or {}).get(role) or {}
            texts = finding_texts + context_texts
            if not any(exact_in(mapping.get('evidence'), t) for t in texts):
                errors.append(f'{role} alignment must quote the selected finding or explicit paper-local context')
            mention = mapping.get('mention')
            if not any(exact_in(mention, t) for t in finding_texts):
                if (mapping.get('kind') in {'explicit_definition', 'context_reference'}
                        and any(exact_in(mention, t) for t in binding_texts)
                        and any(exact_in(mapping.get('evidence'), t) for t in binding_texts)):
                    result.setdefault('alignment_context_roles', []).append(role)
                else:
                    errors.append(f'{role} alignment mention must occur in the finding or an explicitly selected paper-local definition/antecedent')
        for field, value in (candidate.get('qualifiers') or {}).items():
            if not value:
                continue
            mapping = (result.get('qualifier_alignment') or {}).get(field) or {}
            texts = finding_texts + context_texts
            if not any(exact_in(mapping.get('evidence'), t) for t in texts):
                errors.append(f'{field} must cite a literal definition/condition in this paper selection')
            if not any(exact_in(mapping.get('mention'), t) for t in texts):
                errors.append(f'{field} mention is absent from this paper selection')
    return result, list(dict.fromkeys(errors))


def guard_paper(candidate, document, row):
    result, errors = provenance(candidate, document, row)
    decision = result.get('decision')
    if decision in {'supported', 'contradicted'}:
        matches = result.get('matches') or {}
        required = ('subject', 'object', 'relation', 'direction', 'certainty') if decision == 'supported' else ('subject', 'object')
        errors.extend(f'{field} match was not confirmed by the paper judge' for field in required if matches.get(field) is not True)
        if decision == 'contradicted' and not (matches.get('relation') is False or matches.get('direction') is False):
            errors.append('Contradiction needs an explicit incompatible relation/direction, not different study conditions')
        for role in ('subject', 'object'):
            mapping = (result.get('entity_alignment') or {}).get(role) or {}
            if (normalized_phrase(mapping.get('canonical')) != normalized_phrase(candidate.get(role))
                    or mapping.get('kind') not in {'same_concept', 'coordination', 'explicit_definition', 'context_reference'}
                    or not mapping.get('explanation')):
                errors.append(f'{role} identity was not confirmed by the evidence model')
        for field, value in (candidate.get('qualifiers') or {}).items():
            if value:
                mapping = (result.get('qualifier_alignment') or {}).get(field) or {}
                if (normalized_phrase(mapping.get('canonical')) != normalized_phrase(value)
                        or mapping.get('kind') not in {'same_concept', 'coordination', 'explicit_definition', 'context_reference'}
                        or not mapping.get('explanation')):
                    errors.append(f'{field} restriction was not confirmed by the evidence model')
        expected = 'supports' if decision == 'supported' else 'contradicts'
        if any(e.get('stance') != expected for e in result.get('evidence') or []):
            errors.append('Finding stance does not match the paper judgement')
    result['evidence_level'] = document.get('evidence_level', 'independent_external')
    result['retrieval_route'] = document.get('retrieval_route')
    result['independence_group'] = document.get('independence_group')
    result['proof_policy'] = 'paper-local-context-v3'
    if 'context_selections' not in row:
        result['context_selection_status'] = 'not_supplied; no additional context admitted'
    if errors:
        result['rejected_model_decision'] = deepcopy(result)
        result.update(decision='needs_review', evidence=[], matches={},
                      guardrail_reasons=list(dict.fromkeys(errors)),
                      rationale='Paper finding awaits evidence review: ' + '; '.join(dict.fromkeys(errors)))
    return result


def grounded_roles(assessment, documents, allowed=None):
    """Ground the model's contextual type claims without lexical synonym gates."""
    by_id, grounded, errors = {d['evidence_document_id']: d for d in documents}, [], []
    for item in assessment.get('role_evidence') or []:
        doc = by_id.get(item.get('evidence_document_id'))
        if doc is None or item.get('role') not in {'subject', 'object'}:
            errors.append('Role evidence names an unknown paper or role'); continue
        if allowed is not None and not set(item.get('sentence_numbers') or []) <= allowed.get(item.get('evidence_document_id'), {}).get(item.get('chunk_id'), set()):
            errors.append('Role evidence selects sentences absent from the synthesis input'); continue
        selection, error = selected_context(doc, item)
        if error:
            errors.append(error); continue
        if (not any(exact_in(item.get('entity_mention'), text) for text in selection['fragments'])
                or not any(exact_in(item.get('role_trigger'), text) for text in selection['fragments'])
                or not str(item.get('rationale') or '').strip()):
            errors.append('Role classification needs literal entity and role wording in its selected paper context'); continue
        grounded.append({**item, 'quoted_span': selection['quoted_span'], 'fragments': selection['fragments'],
                         'assessed_type': assessment.get(item['role'] + '_type')})
    for role in ('subject', 'object'):
        if not any(r['role'] == role for r in grounded):
            errors.append(f'No quoted contextual evidence for {role} classification')
    return grounded, list(dict.fromkeys(errors))
