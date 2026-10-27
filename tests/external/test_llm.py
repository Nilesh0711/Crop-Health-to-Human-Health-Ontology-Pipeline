"""Direct inference usage accounting and health-check failures."""

from unittest.mock import patch
import pytest

from backend.config.settings import Settings
from backend.providers.inference import InferenceError, normalize_usage
from backend.providers.llm import OpenCodeClient, LLMServiceError, format_request_metrics


def test_partial_usage_does_not_invent_a_tps_estimate():
    metrics = []
    client = OpenCodeClient(Settings(), on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value={
            'content': '{}', 'finish_reason': 'stop', 'usage': {'completion_tokens': 20}}):
        assert client.chat_json('system', 'input') == {}
    assert not metrics[0]['usage_reported']
    assert metrics[0]['effective_output_tps'] is None
    assert 'TPS: N/A' in format_request_metrics(metrics[0])
    assert client.request_metrics['requests_with_usage'] == 0
    assert client.request_metrics['effective_output_tps'] is None


def test_provider_tokens_and_cached_tokens_are_counted_once():
    metrics = []
    client = OpenCodeClient(Settings(), on_request=metrics.append)
    usage = {'prompt_tokens': 100, 'completion_tokens': 47,
             'completion_tokens_details': {'reasoning_tokens': 7},
             'prompt_tokens_details': {'cached_tokens': 80}}
    with patch.object(client._inference, 'chat', return_value={
            'content': '{"claims":[]}', 'finish_reason': 'stop', 'usage': usage}), \
            patch('backend.providers.llm.time.monotonic', side_effect=[10., 14.]):
        assert client.chat_json('system', 'input') == {'claims': []}
    assert metrics[0]['effective_output_tps'] == 10
    assert metrics[0]['effective_generated_tps'] == 11.75
    assert metrics[0]['usage_source'] == 'api'
    assert client.request_metrics['cache_read_tokens'] == 80
    assert client.request_metrics['output_tokens'] == 40
    assert client.request_metrics['requests'] == 1
    assert 'effective output TPS=10.00' in format_request_metrics(metrics[0])


def test_unavailable_catalog_is_reported_as_service_error():
    client = OpenCodeClient(Settings())
    with patch.object(client._inference, 'models', side_effect=InferenceError('HTTP 401')) as request:
        with pytest.raises(LLMServiceError, match='HTTP 401'):
            client.health_check()
    request.assert_called_once()


@pytest.mark.parametrize('usage', [{}, {'completion_tokens': 2}, {'prompt_tokens': -1, 'completion_tokens': 5}])
def test_invalid_or_partial_token_counts_are_not_reported_as_usage(usage):
    assert not normalize_usage(usage)['usage_reported']


@pytest.mark.parametrize('count', [0, 12345])
def test_request_progress_uses_provider_input_tokens_instead_of_character_counts(count):
    progress, metrics = [], []
    client = OpenCodeClient(Settings(), on_progress=progress.append, on_request=metrics.append)
    # Keep the text size unrelated to the provider count; no local estimate should replace it.
    with patch.object(client._inference, 'chat', return_value={
            'content': '{}', 'finish_reason': 'stop', 'usage': {'prompt_tokens': count, 'completion_tokens': 2}}):
        client.chat_json('system', 'evidence' * 100)
    assert progress[0]['input_tokens'] is None and progress[0]['token_count_source'] == 'pending'
    assert progress[1]['input_tokens'] == count and progress[1]['input_tokens_reported']
    assert progress[1]['token_count_source'] == 'provider'
    assert metrics[0]['input_tokens'] == count
    assert f'{count:,} input tokens (provider-reported)' in format_request_metrics(metrics[0])
    assert all('prompt_characters' not in item and 'input_characters' not in item for item in [*progress, *metrics])


def test_input_token_usage_remains_visible_when_output_usage_is_missing():
    events, metrics = [], []
    client = OpenCodeClient(Settings(), on_progress=events.append, on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value={
            'content': '{}', 'finish_reason': 'stop', 'usage': {'prompt_tokens': 321}}):
        client.chat_json('system', 'input')
    assert not metrics[0]['usage_reported']
    assert events[-1]['input_tokens'] == 321
    assert '321 input tokens (provider-reported)' in format_request_metrics(metrics[0])
    assert 'TPS: N/A' in format_request_metrics(metrics[0])
