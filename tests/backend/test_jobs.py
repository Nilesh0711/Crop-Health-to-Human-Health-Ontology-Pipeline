"""Status recovery must not rerun models or lose a job's own audit history."""
import importlib
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from backend.shared.jobs import save_job, read_job
from backend.config.settings import Settings, SCHEMA_VERSION
from backend.shared.storage import file_hash


@pytest.fixture
def api(tmp_path, monkeypatch):
    module = importlib.import_module('backend.api.app')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'placeholder; no PDF processing is requested')
    output = tmp_path / 'output'
    monkeypatch.setattr(module, '_jobs', {})
    monkeypatch.setattr(module, '_pdf_map', lambda: {'paper-id': pdf})
    monkeypatch.setattr(module, '_output_dir', lambda _: output)
    return module, output, TestClient(module.app)


def job(status='running', identifier='job-1'):
    return {'id': identifier, 'pdf_id': 'paper-id', 'pdf_path': 'paper.pdf', 'status': status,
            'logs': [{'level': 'info', 'message': 'Saved extraction: 10 claims'}],
            'options': {'max_pages': 0, 'claims_per_packet': 4},
            'request_progress': {'status': 'waiting_for_provider', 'model': 'primary', 'input_tokens': None}}


def test_finished_job_is_available_after_memory_is_cleared(api):
    module, output, client = api
    original = job('completed')
    original['result'] = {'extracted': 10}
    save_job(output, 'job-1', original)
    with patch.object(module, 'run_pipeline') as run:
        response = client.get('/api/jobs/job-1')
    assert response.status_code == 200
    assert response.json()['status'] == 'completed'
    assert response.json()['result'] == {'extracted': 10}
    assert response.json()['logs'] == [{**row, 'stage': 'claim_extraction'} for row in original['logs']]
    assert 'pdf_path' not in response.json()
    run.assert_not_called()


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_lost_worker_becomes_interrupted_once_and_preserves_extraction(api, status):
    module, output, client = api
    save_job(output, 'job-1', job(status))
    claims = output / 'candidates.jsonl'
    claims.write_text('{"candidate_id":"saved"}\n')
    original = claims.read_bytes()
    with patch.object(module, 'run_pipeline') as run:
        first = client.get('/api/jobs/job-1')
        second = client.get('/api/jobs/job-1')
    assert first.status_code == second.status_code == 200
    result = first.json()
    assert result['status'] == 'failed' and result['interrupted']
    assert 'Resume failed run' in result['error']
    assert result['request_progress']['status'] == 'interrupted'
    assert 'unknown' in result['request_progress']['provider_status']
    assert len(result['logs']) == len(second.json()['logs']) == 2
    assert claims.read_bytes() == original
    assert json.loads((output / 'run_state.json').read_text())['status'] == 'failed'
    assert len((output / 'run_log.jsonl').read_text().splitlines()) == 1
    run.assert_not_called()


def test_old_job_recovery_cannot_replace_latest_run_or_its_logs(api):
    module, output, client = api
    save_job(output, 'job-1', job())
    save_job(output, 'job-2', job('completed', 'job-2'))
    logs = output / 'run_log.jsonl'
    logs.write_text('{"message":"Latest run"}\n')
    response = client.get('/api/jobs/job-1')
    assert response.status_code == 200 and response.json()['interrupted']
    assert json.loads((output / 'run_state.json').read_text())['id'] == 'job-2'
    assert logs.read_text() == '{"message":"Latest run"}\n'
    assert client.get('/api/jobs/job-2').json()['status'] == 'completed'


def test_progress_persists_running_status_and_job_identity(api):
    module, output, client = api
    module._jobs['job-1'] = job()
    event = {'status': 'waiting_for_provider', 'task': 'source check', 'input_tokens': None}
    module._emit('job-1', 'progress', json.dumps(event))
    state = json.loads((output / 'run_state.json').read_text())
    assert state['id'] == 'job-1' and state['status'] == 'running'
    saved = read_job(output, 'job-1')
    assert saved['request_progress'] == event
    assert not any(row.get('level') == 'progress' for row in saved['logs'])
    # A registered worker remains running; disk recovery must not interrupt it.
    assert client.get('/api/jobs/job-1').json()['status'] == 'running'


def test_unknown_job_remains_a_real_404_and_invalid_ids_cannot_escape_output(api):
    _, output, client = api
    assert client.get('/api/jobs/missing').status_code == 404
    assert read_job(output, '../outside') is None
    with pytest.raises(ValueError):
        save_job(output, '../outside', job())


def test_pdf_listing_exposes_saved_job_id_after_restart(api):
    module, output, client = api
    save_job(output, 'job-1', job())
    with patch.object(module, 'resume_available', return_value=True):
        paper = client.get('/api/pdfs').json()[0]
    assert paper['latest_job_id'] == 'job-1'
    assert paper['active_job_id'] is None
    assert paper['status'] == 'failed' and paper['can_resume']


def checkpoint(output, pdf):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'pipeline_checkpoint.json').write_text(json.dumps({
        'schema_version': SCHEMA_VERSION, 'pdf_sha256': file_hash(pdf),
        'task_models': Settings().task_models, 'extraction_complete': True,
        'max_pages': 2, 'claims_per_packet': 3, 'candidate_ids': ['saved']}))
    (output / 'candidates.jsonl').write_text(json.dumps({
        'candidate_id': 'saved', 'subject': 'A', 'object': 'B', 'relation': 'reduces',
        'subject_type': 'Crop', 'object_type': 'PlantTrait', 'schema_error': None,
        'source_pdf_sha256': file_hash(pdf)}) + '\n')


def test_recovery_check_restores_original_extraction_options_without_running_or_writing(api):
    module, output, client = api
    checkpoint(output, module._pdf_map()['paper-id'])
    saved = job('failed')
    saved['options'].update(max_pages=0, claims_per_packet=4, articles_per_claim=12)
    save_job(output, 'job-1', saved)
    original = {p: p.read_bytes() for p in output.rglob('*') if p.is_file()}
    with patch.object(module, 'run_pipeline') as run, patch.object(module.threading, 'Thread') as thread:
        response = client.get('/api/pdfs/paper-id/recovery')
    assert response.status_code == 200 and response.json()['can_continue']
    assert not response.json()['job_missing']
    assert response.json()['options'] == {
        'max_pages': 2, 'max_claims': 0, 'claims_per_packet': 3, 'articles_per_claim': 12,
        'extraction_mode': 'whole_packet', 'extraction_target_words': 800,
        'extraction_generation_budget': 24576, 'thinking': {stage: True for stage in module.ThinkingOptions.model_fields}}
    assert original == {p: p.read_bytes() for p in output.rglob('*') if p.is_file()}
    run.assert_not_called()
    thread.assert_not_called()


def test_recovery_requires_a_valid_checkpoint_and_explains_why_restart_is_needed(api):
    module, output, client = api
    assert not client.get('/api/pdfs/paper-id/recovery').json()['can_continue']
    checkpoint(output, module._pdf_map()['paper-id'])
    module._pdf_map()['paper-id'].write_bytes(b'changed PDF')
    response = client.get('/api/pdfs/paper-id/recovery').json()
    assert not response['can_continue']
    assert 'changed' in response['reason']


def test_missing_job_identity_does_not_prevent_continuation_from_verified_findings(api):
    module, output, client = api
    checkpoint(output, module._pdf_map()['paper-id'])
    (output / 'run_state.json').write_text('{"status":"queued"}')
    response = client.get('/api/pdfs/paper-id/recovery').json()
    assert response['job_missing'] and response['job_id'] is None
    assert response['interrupted'] and response['can_continue']


def test_missing_job_snapshot_is_flagged_even_when_the_id_survives(api):
    module, output, client = api
    checkpoint(output, module._pdf_map()['paper-id'])
    (output / 'run_state.json').write_text('{"id":"missing-job","status":"failed"}')
    response = client.get('/api/pdfs/paper-id/recovery').json()
    assert response['job_missing'] and response['job_id'] == 'missing-job'
    assert response['can_continue']


def test_completed_or_active_jobs_do_not_offer_duplicate_continuation(api):
    module, output, client = api
    checkpoint(output, module._pdf_map()['paper-id'])
    save_job(output, 'job-1', job('completed'))
    assert 'already finished' in client.get('/api/pdfs/paper-id/recovery').json()['reason']
    module._jobs['active'] = job()
    assert 'active run' in client.get('/api/pdfs/paper-id/recovery').json()['reason']


@pytest.mark.parametrize('mode', ['target_context', 'whole_packet'])
def test_run_options_reach_worker_and_are_persisted_without_touching_other_features(api, mode):
    module, output, client = api
    options = dict(extraction_mode=mode, extraction_target_words=600, extraction_generation_budget=32768,
                   claims_per_packet=5, articles_per_claim=9)
    with patch.object(module.OpenCodeClient, 'health_check', return_value={'models': list(Settings().task_models.values())}), \
            patch.object(module.threading, 'Thread'):
        started = client.post('/api/pdfs/paper-id/run', json=options)
    assert started.status_code == 202
    job_id = started.json()['id']
    saved = read_job(output, job_id)
    assert all(saved['options'][key] == value for key, value in options.items())
    with patch.object(module, 'run_pipeline', return_value={'run_status': 'completed'}) as run:
        module._worker(job_id, module.RunOptions(**saved['options']))
    assert run.call_args.kwargs['extraction_mode'] == mode
    assert run.call_args.kwargs['extraction_target_words'] == 600
    assert run.call_args.kwargs['extraction_generation_budget'] == 32768
    assert run.call_args.kwargs['claims_per_packet'] == 5


@pytest.mark.parametrize('provider_available', [True, False])
def test_health_identifies_the_loaded_pipeline_version_even_when_provider_is_unavailable(api, provider_available):
    module, _, client = api
    with patch.object(module.OpenCodeClient, 'health_check',
                      return_value={'models': list(Settings().task_models.values())},
                      side_effect=None if provider_available else RuntimeError('Provider unavailable')):
        result = client.get('/api/health')
    assert result.status_code == 200
    assert result.json()['pipeline']['schema_version'] == SCHEMA_VERSION
    assert result.json()['model_ready'] is provider_available


@pytest.mark.parametrize('option', [dict(extraction_mode='unknown'), dict(extraction_target_words=0),
                                   dict(extraction_generation_budget=1000)])
def test_invalid_extraction_settings_are_rejected_before_any_model_request(api, option):
    _, _, client = api
    assert client.post('/api/pdfs/paper-id/run', json=option).status_code == 422
