"""One independent source reading after a non-positive assessment; never force acceptance."""
from copy import deepcopy
from backend.stages.source_validation.checker import validate_source_claim

POLICY = 'bounded-source-reassessment-v1'


def reassess_source_claim(candidate, client):
    original = deepcopy(candidate['source_validation'])
    reviewing = deepcopy(candidate)
    reviewing['_source_reassessment'] = {
        'previous_decision': original.get('decision'), 'previous_rationale': original.get('rationale'),
        'instruction': 'Independently reassess the exact assertion against the supplied original evidence and local context. '
                       'Identify the disputed endpoint, direction, modality or condition. You may confirm the original '
                       'decision, find entailment, or retain needs_review. Do not search for a favorable answer, '
                       'add outside knowledge, rewrite the scientific assertion, or infer missing causal links.'}
    result = validate_source_claim(reviewing, client)
    if result['decision'] == 'entailed' and result.get('applied_name_corrections'):
        for key in ('subject', 'object', 'relation', 'claim_text', 'qualifiers', 'source_context'):
            if key in reviewing:
                candidate[key] = deepcopy(reviewing[key])
    result['source_review_audit'] = {'policy': POLICY, 'original': original, 'reassessment': deepcopy(result)}
    return result
