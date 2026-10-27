"""Regression coverage for primary-model failures, PDFs and pathway guards."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.stages.pdf_parsing.packets import iter_pdf_context_packets, pdf_bibliography
from backend.config.settings import GO_PIPELINE_MODELS, Settings
from backend.providers.inference import InferenceError
from backend.providers.llm import LLMError, LLMServiceError, OpenCodeClient
from backend.stages.literature_retrieval.clients import fetch_open_access_pdf_pages


def response(value=None):
    return {'content': json.dumps(value if value is not None else {'ok': True}), 'finish_reason': 'stop'}


@pytest.mark.parametrize('status', [429, '429', 408, 500, 502, 503, 504, 404, 400, 401, 403])
def test_provider_failure_stops_on_primary_and_logs_original_error(status):
    metrics = []
    client = OpenCodeClient(Settings(), task='extraction', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=InferenceError(f'HTTP {status}')) as run:
        for _ in range(2):
            with pytest.raises(LLMServiceError, match=f'HTTP {status}'):
                client.chat_json('rules', 'claim')
    assert run.call_count == 1
    assert len(metrics) == 1
    assert metrics[0]['model'] == Settings().extraction_model
    assert metrics[0]['attempt'] == 1
    assert metrics[0]['next_model'] is None
    assert f'HTTP {status}' in metrics[0]['error']


def test_timeout_stops_without_inventing_usage_or_retrying():
    metrics = []
    client = OpenCodeClient(Settings(), on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=InferenceError('Request timed out', 'timeout')) as run:
        with pytest.raises(LLMServiceError, match='timed out'):
            client.chat_json('rules', 'claim')
    assert run.call_count == 1
    assert metrics[0]['status'] == 'timeout'
    assert metrics[0]['next_model'] is None
    assert metrics[0]['effective_output_tps'] is None


def test_scientific_response_and_malformed_json_never_trigger_model_shopping():
    client = OpenCodeClient(Settings(json_repair_attempts=0, json_regeneration_attempts=0))
    with patch.object(client._inference, 'chat', return_value=response({'decision': 'unsupported'})) as run:
        assert client.chat_json('rules', 'claim') == {'decision': 'unsupported'}
        assert run.call_count == 1
    with patch.object(client._inference, 'chat', return_value={'content': 'broken', 'finish_reason': 'stop'}) as run:
        with pytest.raises(LLMServiceError, match='JSON'):
            client.chat_json('rules', 'claim')
        assert run.call_count == 1


def test_concurrent_judges_use_only_primary_and_keep_unique_metrics():
    metrics = []
    settings = Settings()
    client = OpenCodeClient(settings, on_request=metrics.append)
    barrier = threading.Barrier(3)

    def run(model, *args):
        barrier.wait(timeout=5)
        assert model == settings.extraction_model
        return response()

    with patch.object(client._inference, 'chat', side_effect=run), ThreadPoolExecutor(max_workers=3) as pool:
        assert all(result['ok'] for result in pool.map(lambda _: client.chat_json('rules', 'claim'), range(3)))
    assert sorted(m['request_id'] for m in metrics) == [1, 2, 3]
    assert all(m['attempt'] == 1 and m['next_model'] is None for m in metrics)


def test_explicit_primary_overrides_still_require_allowed_model_ids():
    for model in GO_PIPELINE_MODELS:
        assert replace(Settings(), extraction_model=model).extraction_model == model
    for model in ('opencode/deepseek-v4.1-flash', 'opencode/muse-spark-1.3', 'qwen3:8b'):
        with pytest.raises(ValueError):
            replace(Settings(), extraction_model=model)


def test_spaced_reference_heading_preserves_body_and_excludes_titles(tmp_path):
    pages = [SimpleNamespace(extract_text=lambda **kw: 'Abstract\nCrop R samples contained compound T in this study.\nReF eReNCeS\n1. Author. Crop Z contained compound X. 2020.'),
             SimpleNamespace(extract_text=lambda **kw: '2. Author. Crop Y contained compound V. 2021.')]
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=SimpleNamespace(pages=pages)):
        packets = list(iter_pdf_context_packets(tmp_path / 'paper.pdf'))
        bibliography = pdf_bibliography(tmp_path / 'paper.pdf')
    text = ' '.join(s['text'] for p in packets for s in p['sentences'])
    assert 'compound T' in text
    assert 'compound X' not in text and 'compound V' not in text
    assert 1 in bibliography and 2 in bibliography


def test_public_pdf_url_with_spaces_is_encoded_and_invalid_transport_is_nonfatal():
    url = 'https://example.org/VOL. 12/Already%20encoded/Drilling Waste.pdf'
    with patch('backend.stages.literature_retrieval.clients.urllib.request.urlopen', side_effect=ValueError('Bad URL')) as fetch:
        assert fetch_open_access_pdf_pages(url, Settings()) == []
    assert fetch.call_args.args[0].full_url == 'https://example.org/VOL.%2012/Already%20encoded/Drilling%20Waste.pdf'


