"""Generation headroom and recovery severity without changing scientific inputs."""
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError, format_request_metrics, request_log_level
from tests.external.test_inference_api import response, VALID, ANSWER, SCHEMA, BROKEN, PATCH
import json


def test_joint_request_reserves_reasoning_headroom_before_the_first_attempt():
    settings = Settings(external_min_output_budget=28192, thinking={'external': True})
    metrics, events = [], []
    client = OpenCodeClient(settings, task='external', on_request=metrics.append, on_progress=events.append)
    evidence = json.dumps({'documents': [{'id': i, 'text': f'Original evidence {i}'} for i in range(8)]})
    with patch.object(client._inference, 'chat', side_effect=[response(VALID, 'length'), response(VALID)]) as call:
        assert client.chat_json('Original evidence rules', evidence, max_tokens=10000, json_schema=SCHEMA) == ANSWER
    first, second = [item.args for item in call.call_args_list]
    assert first[4] == 28192 and second[4] == 32768
    assert first[0] == second[0] == settings.external_model
    assert first[2] == second[2] == evidence and first[3] == second[3] == SCHEMA
    assert [request_log_level(m) for m in metrics] == ['warning', 'info']
    assert metrics[0]['status'] == 'incomplete_response' and metrics[0]['recovery_pending']
    assert metrics[0]['recovery_action'] == 'response_regeneration'
    assert metrics[1]['recovery_completed'] and not metrics[1]['recovery_pending']
    assert 'regenerating response (recovery 1/1)' in format_request_metrics(metrics[0])
    assert 'recovery completed successfully' in format_request_metrics(metrics[1])
    assert events[0]['output_limit_tokens'] == 28192
    assert events[1]['recovery_pending'] and 'regenerating response' in events[1]['provider_status']


def test_exhausted_recovery_stops_with_a_terminal_error_and_no_third_call():
    metrics = []
    client = OpenCodeClient(Settings(), task='external', on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value=response(VALID, 'length')) as call:
        with pytest.raises(LLMServiceError, match='truncated'):
            client.chat_json('rules', 'all original papers', json_schema=SCHEMA)
    assert call.call_count == 2
    assert [request_log_level(m) for m in metrics] == ['warning', 'error']
    assert metrics[-1]['status'] == 'failed' and not metrics[-1]['recovery_pending']
    assert not metrics[-1]['recovery_completed']


@pytest.mark.parametrize('content,setting', [(VALID, 'json_regeneration_attempts'), (BROKEN, 'json_repair_attempts')])
def test_disabled_recovery_is_an_error_immediately(content, setting):
    metrics = []
    client = OpenCodeClient(Settings(**{setting: 0}), on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value=response(content, 'length' if content == VALID else 'stop')) as call:
        with pytest.raises(LLMServiceError):
            client.chat_json('rules', 'evidence', json_schema=SCHEMA)
    call.assert_called_once()
    assert request_log_level(metrics[0]) == 'error' and not metrics[0]['recovery_pending']


def test_syntax_repair_is_a_warning_then_a_successful_repair_notice():
    metrics = []
    client = OpenCodeClient(Settings(), task='source', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=[response(BROKEN), response(json.dumps(PATCH))]):
        assert client.chat_json('rules', 'evidence', json_schema=SCHEMA) == ANSWER
    assert request_log_level(metrics[0]) == 'warning'
    assert metrics[0]['recovery_action'] == 'format_repair'
    assert 'repairing response formatting' in format_request_metrics(metrics[0])
    assert metrics[1]['recovery_completed']


@pytest.mark.parametrize('task,expected', [('extraction', 24576), ('source', 8192), ('query', 8192)])
def test_extraction_gets_more_room_without_changing_source_or_query_budgets(task, expected):
    client = OpenCodeClient(Settings(), task=task)
    with patch.object(client._inference, 'chat', return_value=response(VALID)) as call:
        client.chat_json('rules', 'evidence', max_tokens=1000, json_schema=SCHEMA)
    assert call.call_args.args[4] == expected


def test_external_floor_is_configurable_and_does_not_reduce_a_larger_request():
    client = OpenCodeClient(Settings(external_min_output_budget=20000), task='external')
    with patch.object(client._inference, 'chat', return_value=response(VALID)) as call:
        client.chat_json('rules', 'evidence', max_tokens=1000, json_schema=SCHEMA)
        client.chat_json('rules', 'evidence', max_tokens=22000, json_schema=SCHEMA)
    assert [c.args[4] for c in call.call_args_list] == [20000, 26096]
    with pytest.raises(ValueError, match='EXTERNAL_MIN_OUTPUT_BUDGET'):
        Settings(external_min_output_budget=8191)
