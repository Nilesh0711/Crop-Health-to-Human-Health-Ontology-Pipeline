"""Single-model stage routing and frontend-visible failure coverage."""

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.providers.inference import InferenceError


def test_each_stage_has_only_its_assigned_primary():
    settings = Settings()
    assert set(settings.task_models) == {'extraction', 'source', 'query', 'external', 'pathway_assembly'}
    assert len(set(settings.task_models.values())) == 3
    with pytest.raises(ValueError, match='Unknown LLM task'):
        OpenCodeClient(settings, settings.source_model, task='invented')


@pytest.mark.parametrize('task', list(Settings().task_models))
def test_every_task_reports_failure_without_switching_models(task):
    primary = Settings().task_models[task]
    metrics = []
    client = OpenCodeClient(Settings(), primary, task=task, on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=InferenceError('HTTP 429')) as run:
        with pytest.raises(LLMServiceError, match=task + ' model ' + primary):
            client.chat_json('Evidence rules', 'Scientific input')
    assert run.call_count == 1
    assert metrics[0]['model'] == primary
    assert metrics[0]['stage'] == task
    assert metrics[0]['next_model'] is None


def test_legacy_environment_cannot_enable_fallbacks_or_waiting():
    with patch.dict('os.environ', {'OPEN_CODE_FALLBACK_MODELS': 'opencode/big-pickle',
                                  'OPEN_CODE_SOURCE_FALLBACK_MODELS': 'opencode/mimo-v2.6-flash-free',
                                  'OPEN_CODE_WAIT_FOR_MODELS': '1'}):
        settings = Settings()
        assert settings.task_models == Settings().task_models
        assert not hasattr(settings, 'wait_for_models')
        assert not hasattr(settings, 'fallback_models')


def test_health_and_start_require_primary_even_if_backup_is_available(tmp_path):
    module = importlib.import_module('backend.api.app')
    with patch.object(module.OpenCodeClient, 'health_check', return_value={'models': ['opencode/big-pickle']}), \
            patch.object(module, '_pdf_path', return_value=tmp_path / 'test.pdf'), \
            patch.object(module, '_jobs', {}), patch.object(module.threading, 'Thread') as thread:
        health = module.health()
        assert not health['model_ready']
        assert not any(health['pipeline']['task_ready'].values())
        assert health['pipeline']['model_policy'] == 'primary_only'
        with pytest.raises(HTTPException) as error:
            module.start_run('pdf-test', module.RunOptions())
        assert error.value.status_code == 400
        assert Settings().extraction_model in error.value.detail
        thread.assert_not_called()


def test_worker_persists_model_error_for_frontend_job_and_activity_log(tmp_path):
    module = importlib.import_module('backend.api.app')
    job = {'id': 'test', 'pdf_id': 'pdf-test', 'pdf_path': str(tmp_path / 'test.pdf'),
           'status': 'queued', 'logs': []}
    message = 'external model opencode-go/mimo-v2.6-pro failed: HTTP 429'
    with patch.object(module, '_jobs', {'test': job}), \
            patch.object(module, '_output_dir', return_value=tmp_path / 'output'), \
            patch.object(module, 'run_pipeline', side_effect=LLMServiceError(message)):
        module._worker('test', module.RunOptions())
    assert job['status'] == 'failed'
    assert job['error'] == message
    assert job['logs'][-1]['level'] == 'error'
    assert job['logs'][-1]['message'] == message
    saved = [json.loads(line) for line in (tmp_path / 'output' / 'run_log.jsonl').read_text(encoding='utf-8').splitlines()]
    assert saved[-1]['message'] == message
