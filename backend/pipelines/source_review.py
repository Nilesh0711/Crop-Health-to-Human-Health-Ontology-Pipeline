"""Persist a human decision and refresh saved views without invoking models."""
import uuid

from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl
from backend.pipelines.claim_repair import refresh_saved_views
from backend.stages.source_validation.review import apply_manual_source_review, source_review_state, source_fidelity_decision


def save_manual_source_review(pdf, output, candidate_id, dimension, decision, *, expected_revision, note=''):
    candidates = read_jsonl(output / 'candidates.jsonl')
    candidate = next((c for c in candidates if c['candidate_id'] == candidate_id), None)
    if candidate is None:
        raise LookupError('Claim not found')
    event = apply_manual_source_review(candidate, dimension, decision,
                                      expected_revision=expected_revision, note=note)
    event['event_id'] = uuid.uuid4().hex
    save_rows(output / 'source_review_history.jsonl', read_jsonl(output / 'source_review_history.jsonl') + [event])
    save_rows(output / 'source_validation.jsonl', [c['source_validation'] for c in candidates if c.get('source_validation')])
    save_rows(output / 'candidates.jsonl', candidates)
    save_rows(output / 'source_entailed_candidates.jsonl', [{'candidate_id': c['candidate_id']}
              for c in candidates if source_fidelity_decision(c) == 'entailed'])
    # Rejection removes affected routes immediately; acceptance requires explicit
    # assembly/resume. Refresh never creates new model assessments.
    summary = refresh_saved_views(pdf, output)
    log = {'at': event['at'], 'level': 'info', 'stage': 'source_validation', 'candidate_id': candidate_id,
           'message': f"Manual {dimension.replace('_', ' ')} review: {decision} | {candidate_id}; no model call"}
    save_rows(output / 'run_log.jsonl', read_jsonl(output / 'run_log.jsonl') + [log])
    return {'candidate_id': candidate_id, 'source_review': source_review_state(candidate), 'summary': summary,
            'resume_required': True, 'message': 'Manual decision saved. Resume the saved pipeline to rebuild and assess affected pathways.'}
