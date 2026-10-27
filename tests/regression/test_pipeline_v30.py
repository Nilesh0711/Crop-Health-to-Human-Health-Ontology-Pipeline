"""Budgeted Go routing keeps article evidence and model attribution separate."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import threading

import pytest

from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.stages.external_validation.workflow import validate_candidate
from backend.providers.inference import InferenceError
from tests.regression.test_pipeline_v28 import response


def article(identifier, score, level='independent_external'):
    return {'evidence_document_id': identifier, 'hybrid_retrieval_score': score,
            'evidence_level': level, 'independence_group': identifier,
            'evidence_chunks': [{'chunk_id': identifier + ':0', 'text': 'Synthetic evidence.'}]}


def test_go_routes_use_only_main_model_for_each_task():
    settings = Settings()
    for task in ('query',):
        assert settings.task_models[task] == 'opencode-go/mimo-v2.6-flash'
    for task in ('extraction', 'source'):
        assert settings.task_models[task] == 'opencode-go/deepseek-v4.1-flash'
    assert settings.external_model == 'opencode-go/mimo-v2.6-pro'
    assert set(settings.task_models) == {'extraction', 'source', 'query', 'external', 'pathway_assembly'}
    for invalid in ('opencode/mimo-v2.6-pro', 'opencode/deepseek-v4.1-flash'):
        with pytest.raises(ValueError):
            replace(settings, external_model=invalid)
    assert settings.external_model == 'opencode-go/mimo-v2.6-pro'


def test_joint_assessment_uses_one_client_for_all_documents():
    documents = [article('cite', 99, 'source_reference'), article('second', .03), article('top', .05)]
    client = SimpleNamespace(model='pro')
    decisions = [{'evidence_document_id': d['evidence_document_id'], 'decision': 'insufficient'} for d in documents]
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': documents}), \
            patch('backend.stages.external_validation.workflow.judge_evidence_pack', return_value=(decisions, {'policy': 'joint_multi_paper'})) as judge:
        result = validate_candidate({'candidate_id': 'synthetic'}, client, None, Settings(external_judgement_mode='joint'))
    judge.assert_called_once()
    assert judge.call_args.args[2] == documents
    assert result['attempts'][0]['judge_allocation']['policy'] == 'joint_multi_paper'
    assert result['validation_status'] == 'external_evidence_not_found'


def test_thread_local_metrics_keep_parallel_paper_attribution():
    client = OpenCodeClient(Settings(), Settings().external_model, task='external')
    barrier = threading.Barrier(2)
    observed = {}

    def worker(label):
        client.chat_json('Synthetic rules', 'Synthetic input', request_label=label)
        barrier.wait(timeout=3)
        observed[label] = client.last_request_metrics['task']

    with patch.object(client._inference, 'chat', return_value=response()):
        threads = [threading.Thread(target=worker, args=(label,)) for label in ('paper-A', 'paper-B')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not any(thread.is_alive() for thread in threads)
    assert observed == {'paper-A': 'paper-A', 'paper-B': 'paper-B'}
    assert client.last_request_metrics is None
    assert client.request_metrics['requests'] == 2


def test_external_client_failure_propagates_without_flash_substitution():
    settings = Settings()
    metrics = []
    client = OpenCodeClient(settings, settings.external_model, task='external', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=InferenceError('HTTP 429')) as run:
        with pytest.raises(LLMServiceError, match='HTTP 429'):
            client.chat_json('Synthetic rules', 'Synthetic paper')
    assert run.call_count == 1
    assert [item['model'] for item in metrics] == [settings.external_model]
    assert client.last_request_metrics['next_model'] is None


def test_global_region_restriction_is_reported_without_changing_model_or_privacy():
    error = InferenceError('HTTP 400: This Go model requires Global regions.')
    settings = Settings()
    metrics = []
    client = OpenCodeClient(settings, settings.source_model, task='source', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=error) as run:
        with pytest.raises(LLMServiceError, match='requires Global regions'):
            client.chat_json('Synthetic rules', 'Synthetic evidence')
    assert run.call_count == 1
    assert metrics[0]['model'] == settings.source_model
    assert metrics[0]['status'] == 'failed'
    assert metrics[0]['next_model'] is None
