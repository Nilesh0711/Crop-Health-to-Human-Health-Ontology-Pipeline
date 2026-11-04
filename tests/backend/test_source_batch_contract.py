"""Generic source envelopes and actionable schema feedback without local repairs."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.shared.batching import iter_batched_judgments
from backend.stages.source_validation.checker import validate_source_claim
from tests.backend.test_source_project_scope import example


def fixtures():
    first, answer = example()
    first.update(candidate_id='stored-claim-one', source_section_context='Factor A and Process B are defined in this exact original section.')
    second = deepcopy(first)
    second['candidate_id'] = 'stored-claim-two'
    return [first, second], {'results': {'C1': deepcopy(answer), 'C2': deepcopy(answer)}}


def run_batch(monkeypatch, responses):
    claims, _ = fixtures()
    calls, metrics = [], []
    client = OpenCodeClient(replace(Settings(), thinking={'source': False}), task='source', on_request=metrics.append)
    replies = iter(responses)
    def chat(model, system, user, schema, limit):
        calls.append((model, system, user, schema, limit))
        answer = next(replies)
        return {'content': answer if isinstance(answer, str) else json.dumps(answer), 'finish_reason': 'stop',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 60, 'completion_tokens_details': {'reasoning_tokens': 0}}}
    monkeypatch.setattr(client._inference, 'chat', chat)
    def run():
        return list(iter_batched_judgments(claims, client, validate_source_claim, keyed=True, label='Source'))
    return claims, calls, metrics, run


def test_keyed_source_results_bind_to_stored_claims_without_copying_long_ids(monkeypatch):
    _, answer = fixtures()
    claims, calls, metrics, run = run_batch(monkeypatch, [answer])
    original = deepcopy(claims)
    results = run()
    assert len(calls) == 1
    assert [result['candidate_id'] for _, result in results] == [c['candidate_id'] for c in claims]
    assert all(result['decision'] == 'entailed' and result['project_scope']['decision'] == 'in_scope' for _, result in results)
    assert claims == original
    payload = json.loads(calls[0][2])
    assert [item['item_id'] for item in payload['items']] == ['C1', 'C2']
    assert 'stored-claim-one' not in calls[0][2] and 'stored-claim-two' not in calls[0][2]
    assert 'candidate_id' not in calls[0][3]['$defs']['assessment']['properties']
    assert calls[0][3]['properties']['results']['required'] == ['C1', 'C2']
    assert len(payload['source_contexts']) == 1
    assert payload['items'][0]['input']['nearby_context_ref'] == payload['items'][1]['input']['nearby_context_ref']
    assert next(iter(payload['source_contexts'].values())) == claims[0]['source_section_context']


@pytest.mark.parametrize('empty_value', ['', None])
def test_unexpected_annotations_get_exact_feedback_and_bounded_same_model_regeneration(monkeypatch, empty_value):
    _, answer = fixtures()
    invalid = deepcopy(answer)
    invalid['results']['C1']['unrequested_annotation'] = empty_value
    before = deepcopy(invalid)
    claims, calls, metrics, run = run_batch(monkeypatch, [invalid, answer])
    assert all(result['decision'] == 'entailed' for _, result in run())
    assert len(calls) == 2 and invalid == before
    assert calls[0][2] == calls[1][2]  # Original evidence, not a reconstruction or a stripped answer.
    assert calls[0][3] == calls[1][3]
    assert all(call[0] == Settings().source_model for call in calls)
    assert 'unexpected fields ["unrequested_annotation"]' in calls[1][1]
    assert 'allowed fields' in calls[1][1]
    assert metrics[1]['purpose'] == 'response_regeneration'


def test_repeated_invalid_annotations_still_fail_instead_of_being_ignored(monkeypatch):
    _, answer = fixtures()
    answer['results']['C1']['unrequested_annotation'] = None
    claims, calls, metrics, run = run_batch(monkeypatch, [answer, answer])
    before = deepcopy(claims)
    with pytest.raises(LLMServiceError, match='unrequested_annotation'):
        run()
    assert len(calls) == 2 and claims == before


def test_duplicate_alias_keys_are_never_silently_overwritten(monkeypatch):
    _, answer = fixtures()
    duplicate = '{"results":{"C1":' + json.dumps(answer['results']['C1']) + ',"C1":' + json.dumps(answer['results']['C1']) + ',"C2":' + json.dumps(answer['results']['C2']) + '}}'
    _, calls, _, run = run_batch(monkeypatch, [duplicate, answer])
    assert len(run()) == 2
    assert len(calls) == 2 and 'Duplicate JSON property: C1' in calls[1][1]


def test_extra_identity_fields_are_rejected_inside_assessments(monkeypatch):
    _, answer = fixtures()
    invalid = deepcopy(answer)
    invalid['results']['C1']['candidate_id'] = 'a-different-claim'
    _, calls, _, run = run_batch(monkeypatch, [invalid, answer])
    results = run()
    assert 'unexpected fields ["candidate_id"]' in calls[1][1]
    assert results[0][1]['candidate_id'] == 'stored-claim-one'


def test_missing_item_is_regenerated_with_its_exact_required_identity(monkeypatch):
    _, answer = fixtures()
    invalid = deepcopy(answer)
    invalid['results'].pop('C2')
    _, calls, _, run = run_batch(monkeypatch, [invalid, answer])
    assert len(run()) == 2
    assert 'missing fields ["C2"]' in calls[1][1]


def test_coordination_alignment_without_explicit_evidence_heals_and_binds(monkeypatch):
    _, answer = fixtures()
    coordination_answer = deepcopy(answer)
    coordination_answer['results']['C1']['entity_alignment']['object'] = {
        'mention': 'Process B',
        'kind': 'coordination',
        'explanation': 'Process B is coordinated in the outcome list.'
    }
    # Deliberately omit 'evidence' field - it should auto-heal to mention
    claims, calls, metrics, run = run_batch(monkeypatch, [coordination_answer])
    results = run()
    assert len(results) == 2
    assert results[0][1]['entity_alignment']['object']['evidence'] == 'Process B'
