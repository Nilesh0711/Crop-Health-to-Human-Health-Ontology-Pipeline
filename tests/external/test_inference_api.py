import json
from pathlib import Path

import httpx
import pytest

from backend.config.settings import Settings
from backend.providers.inference import InferenceHTTP, InferenceError, normalize_usage
from backend.providers.llm import OpenCodeClient, LLMServiceError, format_request_metrics
from backend.providers.response_repair import apply_separator_patch
from backend.providers.structured_output import StructuredOutputError, DuplicateJSONPropertyError, parse_json_object


SCHEMA = {'type': 'object', 'additionalProperties': False,
          'required': ['candidate_id', 'decision', 'quote'], 'properties': {
              'candidate_id': {'type': 'string'},
              'decision': {'type': 'string', 'enum': ['entailed', 'needs_review']},
              'quote': {'type': 'string'}}}
ANSWER = {'candidate_id': 'claim-1', 'decision': 'needs_review',
          'quote': 'Lead reduced microbial activity under the tested conditions.'}
VALID = json.dumps(ANSWER, separators=(',', ':'))
BROKEN = VALID.replace(',"decision"', ' "decision"')
PATCH = {'edits': [{'before': '"claim-1" "decision"', 'after': '"claim-1","decision"'}]}


def response(content, finish='stop'):
    return {'content': content, 'finish_reason': finish,
            'usage': {'prompt_tokens': 100, 'completion_tokens': 30,
                      'completion_tokens_details': {'reasoning_tokens': 10}},
            'provider_request_id': 'request-test', 'resolved_model': 'deepseek-v4.1-flash',
            'response_format': 'json_object'}


def client_with_responses(monkeypatch, replies, tmp_path=None, **settings):
    requests, metrics = [], []
    client = OpenCodeClient(Settings(**settings), task='source',
                            on_request=metrics.append, response_dir=tmp_path)
    iterator = iter(replies)
    def chat(*args):
        requests.append(args)
        return next(iterator)
    monkeypatch.setattr(client._inference, 'chat', chat)
    return client, requests, metrics


def test_one_bounded_same_model_repair_preserves_values_and_archives_attempts(monkeypatch, tmp_path):
    client, calls, metrics = client_with_responses(monkeypatch,
        [response(BROKEN), response(json.dumps(PATCH))], tmp_path)
    assert client.chat_json('verify', 'source text', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and calls[0][0] == calls[1][0] == client.model
    assert [m['status'] for m in metrics] == ['format_error', 'ok']
    assert [m['purpose'] for m in metrics] == ['judgement', 'format_repair']
    assert all(m['next_model'] is None for m in metrics)
    assert client.request_metrics['requests'] == 2
    assert client.request_metrics['output_tokens'] == 40  # Reasoning counted separately.
    assert 'same-model formatting repair' in format_request_metrics(metrics[-1])
    original = json.loads(Path(metrics[0]['response_artifact']).read_text())
    repaired = json.loads(Path(metrics[1]['response_artifact']).read_text())
    assert original['content'] == BROKEN and original['accepted_response'] is None
    assert repaired['accepted_response'] == ANSWER
    assert 'test-only-key' not in json.dumps(metrics) + json.dumps(original) + json.dumps(repaired)


@pytest.mark.parametrize('field,value', [('candidate_id', 'another-claim'),
                                       ('decision', 'entailed'), ('quote', 'Invented evidence')])
def test_repair_cannot_change_identifiers_decisions_or_quotes(monkeypatch, field, value):
    modified = {**ANSWER, field: value}
    patch = {'edits': [{'before': BROKEN, 'after': json.dumps(modified, separators=(',', ':'))}]}
    client, calls, metrics = client_with_responses(monkeypatch, [response(BROKEN), response(json.dumps(patch))])
    with pytest.raises(LLMServiceError, match='change values'):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 2 and metrics[-1]['status'] == 'failed'


@pytest.mark.parametrize('content,finish', [('', 'stop'), (VALID, 'length'), (VALID[:-8], 'stop'),
                                          ('{"quote":', 'stop')])
def test_missing_content_regenerates_from_original_input_with_same_model(monkeypatch, content, finish):
    client, calls, metrics = client_with_responses(monkeypatch, [response(content, finish), response(VALID)])
    assert client.chat_json('verify', 'original source evidence', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == client.model
    assert calls[0][2] == calls[1][2] == 'original source evidence'
    assert calls[0][3] == calls[1][3] == SCHEMA
    assert calls[1][4] >= calls[0][4]
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert metrics[0]['status'] == 'incomplete_response'
    assert 'same-model regeneration' in format_request_metrics(metrics[1])


def test_filtered_answer_does_not_trigger_either_recovery_path(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response(VALID, 'content_filter')])
    with pytest.raises(LLMServiceError):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 1


def test_invalid_repair_is_not_repaired_again(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response(BROKEN), response('{broken')])
    with pytest.raises(LLMServiceError):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 2 and metrics[-1]['status'] == 'failed'


def test_missing_required_scientific_fields_trigger_regeneration_not_editing(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response('{"decision":"entailed"}'), response(VALID)])
    assert client.chat_json('verify', 'source', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and metrics[-1]['purpose'] == 'response_regeneration'


@pytest.mark.parametrize('duplicate_value', ['claim-1', 'another-claim'])
def test_duplicate_properties_regenerate_without_choosing_first_or_last_values(monkeypatch, duplicate_value):
    duplicate = VALID[:-1] + ',"candidate_id":' + json.dumps(duplicate_value) + '}'
    client, calls, metrics = client_with_responses(monkeypatch, [response(duplicate), response(VALID)])
    assert client.chat_json('verify', 'original PDF evidence', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2
    assert calls[0][2] == calls[1][2] == 'original PDF evidence'
    assert calls[1][3] == SCHEMA
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert metrics[0]['status'] == 'schema_error'
    assert 'Duplicate JSON property: candidate_id' in metrics[0]['error']
    assert all(not m['response_format_adjustments'] for m in metrics)
    assert client.thinking_enabled is False


def test_nested_duplicate_properties_are_detected_but_separate_result_objects_are_allowed():
    with pytest.raises(DuplicateJSONPropertyError, match='canonical'):
        parse_json_object('{"results":[{"alignment":{"canonical":"A","canonical":"B"}}]}')
    valid = '{"results":[{"candidate_id":"A"},{"candidate_id":"B"}]}'
    assert parse_json_object(valid)[0] == {'results': [{'candidate_id': 'A'}, {'candidate_id': 'B'}]}


def test_three_claim_source_batch_with_repeated_id_properties_regenerates_then_runs_source_guards(monkeypatch):
    from backend.shared.batching import iter_batched_judgments
    from backend.stages.source_validation.checker import validate_source_claim
    from tests.regression.test_pipeline_v24 import source_claim, source_reply
    claims = [source_claim(name) for name in ('one', 'two', 'three')]
    claims[-1]['source_evidence_span'] = 'Organic farming was introduced to the study area.'
    rows = [{'candidate_id': c['candidate_id'], **source_reply(), 'claim_text_match': True,
             'occurrence_provenance_fields': []} for c in claims]
    duplicate = '{"results":[' + ','.join(json.dumps(row)[:-1] + ',"candidate_id":' + json.dumps(row['candidate_id']) + '}'
                                         for row in rows) + ']}'
    client, calls, metrics = client_with_responses(monkeypatch, [response(duplicate), response(json.dumps({'results': rows}))])
    results = list(iter_batched_judgments(claims, client, validate_source_claim, label='Source'))
    assert len(calls) == 2 and calls[0][2] == calls[1][2]
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert [c['candidate_id'] for c, _ in results] == ['one', 'two', 'three']
    assert [r['decision'] for _, r in results] == ['entailed', 'entailed', 'needs_review']


def test_repeated_duplicates_exhaust_one_regeneration_and_never_start_format_repair(monkeypatch):
    duplicate = VALID[:-1] + ',"candidate_id":"claim-1"}'
    client, calls, metrics = client_with_responses(monkeypatch, [response(duplicate), response(duplicate)])
    with pytest.raises(LLMServiceError, match='Duplicate JSON property'):
        client.chat_json('verify', 'original PDF evidence', json_schema=SCHEMA)
    assert len(calls) == 2 and metrics[-1]['status'] == 'failed'
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']


def test_duplicate_regeneration_can_be_disabled_without_silently_dropping_fields(monkeypatch):
    duplicate = VALID[:-1] + ',"candidate_id":"claim-1"}'
    client, calls, metrics = client_with_responses(monkeypatch, [response(duplicate)], json_regeneration_attempts=0)
    with pytest.raises(LLMServiceError, match='Duplicate JSON property'):
        client.chat_json('verify', 'original PDF evidence', json_schema=SCHEMA)
    assert len(calls) == 1 and not metrics[0]['recovery_pending']


@pytest.mark.parametrize('before,after,error', [
    ('"claim-1"', '"claim-1"', 'proposes no change'),
    ('missing target', 'replacement', 'target occurs 0 times'),
    ('"', '" ', 'target occurs'),
])
def test_rejected_patch_reports_the_actual_reason(before, after, error):
    with pytest.raises(StructuredOutputError, match=error):
        apply_separator_patch(BROKEN, {'edits': [{'before': before, 'after': after}]}, SCHEMA)


def test_repair_can_be_disabled(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response(BROKEN)], json_repair_attempts=0)
    with pytest.raises(LLMServiceError):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 1


def test_known_misplaced_annotation_brace_requires_a_model_proposal(monkeypatch):
    schema = {'type': 'object', 'required': ['alignment', 'decision', 'quote'], 'additionalProperties': False,
              'properties': {'alignment': {'type': 'object', 'required': ['explanation'], 'additionalProperties': False,
                                          'properties': {'explanation': {'type': 'string'}}},
                             'decision': {'type': 'string'}, 'quote': {'type': 'string'}}}
    valid = '{"alignment":{"explanation":"Context."},"decision":"needs_review","quote":"Exact evidence."}'
    malformed = valid.replace('"Context."}', '"Context.}"')
    patch = {'edits': [{'before': '"explanation":"Context.}"', 'after': '"explanation":"Context."}'}]}
    client, calls, metrics = client_with_responses(monkeypatch, [response(malformed), response(json.dumps(patch))])
    assert client.chat_json('verify', 'source', json_schema=schema) == json.loads(valid)
    assert len(calls) == 2 and metrics[0]['status'] == 'format_error'
    assert metrics[0]['response_format_adjustments'] == []
    assert metrics[1]['response_format_adjustments'][0]['kind'] == 'model_syntax_patch'


def test_complete_values_with_only_a_missing_final_brace_use_model_edits(monkeypatch):
    patch = {'edits': [{'before': VALID[:-1], 'after': VALID}]}
    client, calls, metrics = client_with_responses(monkeypatch, [response(VALID[:-1]), response(json.dumps(patch))])
    assert client.chat_json('verify', 'source', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and metrics[1]['purpose'] == 'format_repair'


def test_markdown_wrapper_is_removed_only_when_the_model_proposes_edits(monkeypatch):
    patch = {'edits': [{'before': '```json\n', 'after': ''}, {'before': '\n```', 'after': ''}]}
    client, calls, metrics = client_with_responses(monkeypatch, [response('```json\n' + VALID + '\n```'), response(json.dumps(patch))])
    assert client.chat_json('verify', 'source', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and metrics[1]['purpose'] == 'format_repair'


@pytest.mark.parametrize('second', [response(BROKEN), response('{"decision":"entailed"}'), response(VALID, 'length')])
def test_failed_regeneration_cannot_start_another_recovery_branch(monkeypatch, second):
    client, calls, metrics = client_with_responses(monkeypatch, [response('', 'stop'), second])
    with pytest.raises(LLMServiceError):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 2 and metrics[1]['purpose'] == 'response_regeneration'
    assert metrics[1]['status'] == 'failed'


def test_regeneration_can_be_disabled_independently_of_edit_proposals(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response('')], json_regeneration_attempts=0)
    with pytest.raises(LLMServiceError):
        client.chat_json('verify', 'source', json_schema=SCHEMA)
    assert len(calls) == 1


def test_complete_negative_verdict_does_not_trigger_regeneration(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response(VALID)])
    assert client.chat_json('verify', 'source', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 1 and not metrics[0]['response_format_adjustments']


def test_a_valid_unsupported_source_answer_can_have_empty_proof_without_regeneration(monkeypatch):
    from backend.shared.batching import iter_batched_judgments
    from backend.stages.source_validation.checker import validate_source_claim
    from tests.regression.test_pipeline_v24 import source_claim, source_reply
    row = {'candidate_id': 'one', **source_reply(), 'claim_text_match': True, 'occurrence_provenance_fields': []}
    row.update(decision='unsupported', proof={'subject': '', 'relation': '', 'object': ''})
    client, calls, metrics = client_with_responses(monkeypatch, [response(json.dumps({'results': [row]}))])
    result = list(iter_batched_judgments([source_claim('one')], client, validate_source_claim))
    assert result[0][1]['decision'] == 'unsupported' and len(calls) == 1


def test_a_nonobject_answer_is_regenerated_instead_of_wrapped_locally(monkeypatch):
    client, calls, metrics = client_with_responses(monkeypatch, [response('[]'), response(VALID)])
    assert client.chat_json('verify', 'original evidence', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and metrics[1]['purpose'] == 'response_regeneration'


def test_missing_source_proof_regenerates_once_and_still_runs_evidence_guards(monkeypatch):
    from backend.shared.batching import iter_batched_judgments
    from backend.stages.source_validation.checker import validate_source_claim
    from tests.regression.test_pipeline_v24 import source_claim, source_reply
    missing = {'candidate_id': 'one', **source_reply(), 'claim_text_match': True, 'occurrence_provenance_fields': []}
    missing['proof']['object'] = ''
    complete = {'candidate_id': 'one', **source_reply(), 'claim_text_match': True, 'occurrence_provenance_fields': []}
    complete['proof']['object'] = 'An invented outcome absent from the source'
    client, calls, metrics = client_with_responses(monkeypatch, [
        response(json.dumps({'results': [missing]})), response(json.dumps({'results': [complete]}))])
    results = list(iter_batched_judgments([source_claim('one')], client, validate_source_claim))
    assert len(calls) == 2 and metrics[1]['purpose'] == 'response_regeneration'
    assert calls[0][2] == calls[1][2]
    assert results[0][1]['decision'] == 'needs_review'


def test_empty_provider_answer_reaches_the_regeneration_coordinator(monkeypatch):
    count = []
    def handler(request):
        count.append(request)
        answer = None if len(count) == 1 else VALID
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}]})
    transport = httpx.MockTransport(handler)
    original_client = httpx.Client
    monkeypatch.setattr('backend.providers.inference.httpx.Client', lambda **kwargs: original_client(transport=transport))
    metrics = []
    client = OpenCodeClient(Settings(), on_request=metrics.append)
    assert client.chat_json('verify', 'original evidence', json_schema=SCHEMA) == ANSWER
    assert len(count) == 2 and metrics[1]['purpose'] == 'response_regeneration'


def test_repaired_batch_still_runs_source_guards_for_each_claim(monkeypatch):
    from backend.shared.batching import iter_batched_judgments
    from backend.stages.source_validation.checker import validate_source_claim
    from tests.regression.test_pipeline_v24 import source_claim, source_reply
    claims = [source_claim('one'), source_claim('two')]
    claims[1]['source_evidence_span'] = 'Organic farming was introduced to the study area.'
    rows = [{'candidate_id': c['candidate_id'], **source_reply(), 'claim_text_match': True,
             'occurrence_provenance_fields': []} for c in claims]
    text = json.dumps({'results': rows}, separators=(',', ':')).replace('"one","decision"', '"one" "decision"')
    patch = {'edits': [{'before': '"one" "decision"', 'after': '"one","decision"'}]}
    client, calls, metrics = client_with_responses(monkeypatch, [response(text), response(json.dumps(patch))])
    results = list(iter_batched_judgments(claims, client, validate_source_claim))
    assert len(calls) == 2
    assert [result['decision'] for _, result in results] == ['entailed', 'needs_review']




def test_repair_cannot_reorganize_nesting_but_can_propose_a_missing_final_brace():
    with pytest.raises(StructuredOutputError):
        apply_separator_patch(BROKEN, {'edits': [{'before': BROKEN, 'after': '[' + VALID + ']'}]}, SCHEMA)
    fixed, _ = apply_separator_patch(VALID[:-1], {'edits': [{'before': VALID[:-1], 'after': VALID}]}, SCHEMA)
    assert fixed == ANSWER


@pytest.mark.parametrize('model,mode', [('deepseek-v4.1-flash', 'json_object'),
                                      ('mimo-v2.6-flash', 'json_schema'), ('mimo-v2.6-pro', 'json_schema')])
def test_direct_request_uses_documented_go_route_and_backend_only_auth(monkeypatch, model, mode):
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={'id': 'test-response', 'model': model,
            'choices': [{'finish_reason': 'stop', 'message': {'content': VALID}}],
            'usage': {'prompt_tokens': 25, 'completion_tokens': 30}})
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr('backend.providers.inference.httpx.Client', lambda **kwargs: http_client)
    transport = InferenceHTTP(Settings())
    result = transport.chat('opencode-go/' + model, 'system JSON', 'input', SCHEMA, 8192)
    request = captured[0]
    payload = json.loads(request.content)
    assert str(request.url) == 'https://opencode.ai/inference/go/openai/v1/chat/completions'
    assert request.headers['Authorization'] == 'Bearer test-only-key'
    assert request.headers['x-opencode-session'] == transport.session_id
    assert payload['model'] == model and payload['response_format']['type'] == mode
    assert 'test-only-key' not in request.content.decode()
    assert result['content'] == VALID


@pytest.mark.parametrize('http_status', [400, 401, 403, 429, 503])
def test_provider_errors_never_retry_switch_models_or_dump_sensitive_bodies(monkeypatch, http_status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(http_status, json={'error': 'test-only-key should never be printed'})
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr('backend.providers.inference.httpx.Client', lambda **kwargs: http_client)
    metrics = []
    client = OpenCodeClient(Settings(), on_request=metrics.append)
    with pytest.raises(LLMServiceError, match=f'HTTP {http_status}') as error:
        client.chat_json('system', 'input', json_schema=SCHEMA)
    assert len(calls) == 1
    assert 'test-only-key' not in str(error.value) + json.dumps(metrics)


def test_usage_does_not_double_count_reasoning_or_invent_missing_counts():
    usage = normalize_usage({'prompt_tokens': 100, 'completion_tokens': 70,
                             'completion_tokens_details': {'reasoning_tokens': 50},
                             'prompt_tokens_details': {'cached_tokens': 80}})
    assert usage['output_tokens'] == 20 and usage['reasoning_tokens'] == 50
    assert usage['cache_read_tokens'] == 80 and usage['reasoning_usage_reported']
    assert not normalize_usage({})['usage_reported']


def test_provider_error_message_is_visible_with_credentials_redacted(monkeypatch):
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        400, json={'error': {'message': 'Unsupported response_format; key=test-only-key'}})))
    monkeypatch.setattr('backend.providers.inference.httpx.Client', lambda **kwargs: http_client)
    with pytest.raises(InferenceError, match='Unsupported response_format') as error:
        InferenceHTTP(Settings()).chat('opencode-go/deepseek-v4.1-flash', 'JSON', 'input', SCHEMA, 8192)
    assert 'test-only-key' not in str(error.value)


def test_bounded_repair_configuration_rejects_unlimited_loops():
    with pytest.raises(ValueError, match='0 or 1'):
        Settings(json_repair_attempts=2)
