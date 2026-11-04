"""Manual source decisions, bound to the exact claim and original model reading.

No inference runs here. Model assessments are never overwritten by a researcher.
"""
from copy import deepcopy
import json

from backend.shared.storage import stable_hash, utc_now


def review_input_signature(candidate):
    source = candidate.get('source_validation') or {}
    return stable_hash(json.dumps({
        'claim': {k: v for k, v in candidate.items() if k not in {'source_validation', 'semantic_validation'}},
        'model_assessment': {k: v for k, v in source.items() if k != 'manual_reviews'},
    }, sort_keys=True, ensure_ascii=False), 64)


def can_accept_source_review(candidate):
    source = candidate.get('source_validation') or {}
    numbers = candidate.get('source_evidence_sentence_numbers')
    return bool(source and not candidate.get('superseded_by') and not source.get('batch_error') and not candidate.get('schema_error')
                and str(candidate.get('source_evidence_span') or '').strip()
                and isinstance(numbers, list) and 1 <= len(numbers) <= 3
                and all(type(n) is int and n > 0 for n in numbers)
                and len(numbers) == len(set(numbers)))


def source_review_state(candidate):
    source = candidate.get('source_validation') or {}
    reviews = source.get('manual_reviews') or {}
    signature = review_input_signature(candidate)
    active, stale = {}, []
    fidelity = source.get('decision', 'not_run')
    scope = (source.get('project_scope') or {}).get('decision', 'not_assessed')
    if scope not in {'in_scope', 'out_of_scope', 'needs_review'}:
        scope = 'not_assessed'
    for dimension, review in reviews.items():
        if dimension not in {'source_fidelity', 'project_scope'}:
            continue
        if review.get('input_signature') != signature or review.get('decision') not in {'accept', 'reject'}:
            stale.append(dimension)
            if dimension == 'source_fidelity':
                fidelity = 'needs_review'
            else:
                scope = 'needs_review'
            continue
        active[dimension] = review
        if dimension == 'source_fidelity':
            fidelity = 'entailed' if review['decision'] == 'accept' else 'unsupported'
        else:
            scope = 'in_scope' if review['decision'] == 'accept' else 'out_of_scope'
    revision = stable_hash(json.dumps([signature, reviews], sort_keys=True, ensure_ascii=False), 64)
    return {'input_signature': signature, 'revision': revision, 'active_reviews': active,
            'stale_dimensions': stale, 'fidelity_decision': fidelity, 'scope_decision': scope,
            'can_accept': can_accept_source_review(candidate)}


def source_fidelity_decision(candidate):
    source = candidate.get('source_validation') or {}
    if not source.get('manual_reviews'):
        return source.get('decision', 'not_run')
    return source_review_state(candidate)['fidelity_decision']


def apply_manual_source_review(candidate, dimension, decision, *, expected_revision, note=''):
    """Mutate only manual metadata; return an immutable audit event for persistence."""
    if dimension not in {'source_fidelity', 'project_scope'} or decision not in {'accept', 'reject'}:
        raise ValueError('Unknown manual review dimension or decision')
    if not candidate.get('source_validation'):
        raise ValueError('Run the initial source check before reviewing this claim')
    state = source_review_state(candidate)
    if expected_revision != state['revision']:
        raise ValueError('The claim or review changed. Refresh the findings before reviewing it again.')
    if decision == 'accept' and not state['can_accept']:
        raise ValueError('Acceptance requires valid claim structure and recorded source evidence sentence IDs; repair missing or invalid evidence first')
    at = utc_now()
    review = {'decision': decision, 'at': at, 'note': note.strip(),
              'input_signature': state['input_signature'], 'origin': 'manual'}
    source = candidate['source_validation']
    event = {'candidate_id': candidate['candidate_id'], 'dimension': dimension, **review,
             'claim': deepcopy({k: v for k, v in candidate.items() if k not in {'source_validation', 'semantic_validation'}}),
             'previous_review': deepcopy((source.get('manual_reviews') or {}).get(dimension)),
             'model_assessment': deepcopy({k: v for k, v in source.items() if k != 'manual_reviews'})}
    source.setdefault('manual_reviews', {})[dimension] = review
    return event
