"""Historical parser wording is presentation metadata, never a data migration."""
from copy import deepcopy
from unittest.mock import patch
import importlib
import json
import pytest

from backend.shared.activity import display_log, display_request, input_token_label, research_activity


MODEL = 'opencode-go/deepseek-v4.1-flash'
OLD_ERROR = 'Qwen returned non-JSON output: \'{"results":[{"decision":"entailed"\''


@pytest.mark.parametrize('stage,message', [
    ('article_fetch', 'Fetching and preparing 8 papers with 4 workers'),
    ('article_ranking', 'Ranking articles: 500 accessible metadata records'),
    ('chunk_preparation', 'Ranked 4 passages: Synthetic paper'),
    ('chunk_preparation', 'Passage preparation completed: 8 papers'),
    ('literature_search', 'Europe PMC queries: pesticide safety'),
    ('literature_search', 'Literature search returned 600 results'),
    ('external_validation', 'External validation: paper 2/8; Synthetic paper'),
    ('external_validation', 'External validation: paper 2/8 saved; supported'),
    ('external_validation', f'OpenCode {MODEL} request #1: ok; 9.5s; input=100'),
    ('external_validation', 'Resuming retained evidence packet; compatible paper judgements will be reused'),
    ('source_validation', 'Source batch 1: 3 claim(s)'),
    ('query_planning', 'Query batch 1: 6 claim(s)'),
    ('claim_extraction', 'Extracting Introduction (pages 1-3), target 1'),
    ('claim_extraction', 'Introduction: 4 accepted candidates so far'),
    ('system', 'Loading semantic reranker BGE'),
    ('system', 'Semantic reranker ready (cuda:0)'),
    ('system', 'LLM external: opencode-go/mimo-v2.6-pro; Thinking off'),
    ('pathway_assembly', 'Pathway Assembly new/changed endpoints 1-24/200; registry scan 1-24/1000'),
])
def test_routine_messages_are_excluded_from_research_activity(stage, message):
    assert not research_activity({'level': 'info', 'stage': stage, 'message': message})
    assert research_activity({'level': 'warning', 'stage': stage, 'message': message})
    assert research_activity({'level': 'error', 'stage': stage, 'message': message})


@pytest.mark.parametrize('level,message', [
    ('stage', 'Checking source evidence for 92 claims'),
    ('info', 'Claim 2/50: Fusarium head blight -> reduces -> grain quality'),
    ('success', 'Claim status: world_independent_supported_pending_corroboration'),
    ('success', 'Source status: entailed | Fusarium head blight -> reduces -> grain quality'),
    ('info', 'Extraction complete: 92 unique candidates'),
    ('info', 'Reusing 3 completed source judgements; 89 claims remain'),
    ('info', 'External validation: cross-paper synthesis for 8 saved paper findings'),
    ('info', 'OpenCode request: recovery completed successfully'),
    ('warning', 'Citation has no unambiguous bibliography link'),
])
def test_research_milestones_outcomes_and_recovery_remain_visible(level, message):
    assert research_activity({'level': level, 'message': message})


def test_routine_activity_is_neither_appended_nor_persisted_and_does_not_change_active_stage(tmp_path):
    app = importlib.import_module('backend.api.app')
    job = {'pdf_path': 'paper.pdf', 'logs': [], 'activity_stage': 'external_validation'}
    with patch.object(app, '_jobs', {'quiet': job}), patch.object(app, '_output_dir', return_value=tmp_path), \
            patch.object(app, '_persist_job') as persist:
        app._emit('quiet', 'info', 'Ranked 4 passages: Synthetic paper')
    assert job['logs'] == [] and job['activity_stage'] == 'external_validation'
    persist.assert_not_called()
    assert not (tmp_path / 'run_log.jsonl').exists()


def test_historical_routine_activity_is_hidden_without_rewriting_saved_logs(tmp_path):
    app = importlib.import_module('backend.api.app')
    rows = [{'level': 'info', 'message': 'Ranked 4 passages: Synthetic paper', 'stage': 'chunk_preparation'},
            {'level': 'warning', 'message': 'Paper fetch unavailable', 'stage': 'article_fetch'},
            {'level': 'success', 'message': 'Claim status: supported'}]
    path = tmp_path / 'run_log.jsonl'; path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
    original = path.read_bytes()
    with patch.object(app, '_pdf_path', return_value=tmp_path/'paper.pdf'), patch.object(app, '_output_dir', return_value=tmp_path):
        assert len(app.pdf_logs('paper')) == 2
    assert len(app._job_snapshot({'logs': rows})['logs']) == 2
    assert path.read_bytes() == original


def test_historical_progress_names_the_recorded_model_without_mutating_saved_data():
    entry = {'level': 'progress', 'message': 'Source validation batch: failed',
             'progress': {'model': MODEL, 'status': 'failed', 'provider_status': OLD_ERROR}}
    original = deepcopy(entry)
    shown = display_log(entry)
    assert 'Qwen' not in shown['progress']['provider_status']
    assert shown['progress']['provider_status'].startswith('Response JSON parsing failed:')
    assert MODEL in shown['historical_error_notice']
    assert 'original log preserved' in shown['progress']['historical_error_notice']
    assert shown['progress']['original_provider_status'] == OLD_ERROR
    assert entry == original


def test_historical_request_and_error_row_keep_the_original_diagnostics():
    row = display_log({'message': f'OpenCode {MODEL} request #4: failed; {OLD_ERROR}'})
    assert MODEL in row['historical_error_notice']
    assert 'Qwen' not in row['message']
    request = {'model': MODEL, 'error': OLD_ERROR, 'status': 'failed'}
    shown = display_request(request)
    assert shown['original_error'] == request['error']
    assert shown['model'] == MODEL and shown['status'] == 'failed'
    assert 'Qwen' not in shown['error']


def test_current_provider_errors_are_preserved():
    row = {'level': 'error', 'message': f'source model {MODEL} failed: HTTP 429'}
    assert display_log(row) == {**row, 'stage': 'source_validation'}
    request = {'model': MODEL, 'error': 'HTTP 429', 'status': 'failed'}
    assert display_request(request) == request


def test_logs_endpoint_and_job_progress_only_change_the_presentation(tmp_path):
    app = importlib.import_module('backend.api.app')
    entry = {'message': OLD_ERROR, 'level': 'error', 'progress': {
        'model': MODEL, 'provider_status': OLD_ERROR, 'status': 'failed'}}
    path = tmp_path / 'run_log.jsonl'
    path.write_text(json.dumps(entry) + '\n', encoding='utf-8')
    original = path.read_bytes()
    with patch.object(app, '_pdf_path', return_value=tmp_path / 'source.pdf'), \
            patch.object(app, '_output_dir', return_value=tmp_path):
        shown = app.pdf_logs('pdf-id')
    assert shown[0]['original_message'] == OLD_ERROR
    assert 'Qwen' not in shown[0]['message']
    assert path.read_bytes() == original
    job = {'logs': [entry], 'error': OLD_ERROR, 'request_progress': entry['progress']}
    snapshot = app._job_snapshot(job)
    assert 'Qwen' not in snapshot['request_progress']['provider_status']
    assert 'Qwen' not in snapshot['error']
    assert job['request_progress']['provider_status'] == OLD_ERROR


def test_saved_character_counts_are_not_presented_as_token_counts():
    row = {'message': 'Source validation: timeout (81000 prompt characters)', 'progress': {
        'task': 'Source validation', 'status': 'timeout', 'prompt_characters': 81000, 'input_characters': 72000}}
    original = deepcopy(row)
    shown = display_log(row)
    assert shown['message'] == 'Source validation: timeout (input tokens unavailable)'
    assert shown['original_message'] == row['message']
    assert row == original
    assert input_token_label({'input_tokens': 0, 'usage_reported': False}) == 'input tokens unavailable'


def test_finished_token_count_is_persisted_in_live_state_without_duplicate_activity_row(tmp_path):
    app = importlib.import_module('backend.api.app')
    event = {'task': 'Source validation', 'status': 'ok', 'input_tokens': 12345, 'input_tokens_reported': True}
    with patch.object(app, '_jobs', {'tokens': {'pdf_path': 'paper.pdf', 'logs': []}}), \
            patch.object(app, '_output_dir', return_value=tmp_path):
        app._emit('tokens', 'progress', json.dumps(event))
    from backend.shared.jobs import read_job
    saved = read_job(tmp_path, 'tokens')
    assert saved['request_progress']['input_tokens'] == 12345
    assert saved['logs'] == []
    assert not (tmp_path / 'run_log.jsonl').exists()
