"""Recover failed stages without repeating extraction or successful judgements."""
import copy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.config.settings import Settings, SCHEMA_VERSION
from backend.pipelines.checkpoints import resume_metadata, save_checkpoint
from backend.providers.llm import LLMServiceError
from backend.shared.artifacts import save_rows
from backend.shared.storage import file_hash, read_jsonl
from backend.stages.external_validation.workflow import validation_signature
from tests.regression.test_pipeline_v24 import source_claim, source_reply


@pytest.fixture
def run_fixture(tmp_path, monkeypatch):
    module = importlib.import_module('backend.pipelines.document')
    pdf = tmp_path / 'sample.pdf'
    pdf.write_bytes(b'PDF fixture; deterministic parsing is supplied below')
    settings = Settings(enable_nli=False, validation_batch_size=1)
    output = tmp_path / 'outputs' / pdf.stem
    calls = {'source': [], 'query': [], 'external': [], 'extraction': 0}
    fail = {'source': None, 'external': None}
    inspection = {'regions': [], 'pages': [], 'total_pages': 1, 'selected_pages': 1,
                  'pages_with_text': 1, 'pages_with_warnings': 0, 'chapter_boundaries': []}

    class Client:
        def __init__(self, _settings, model=None, task=None, **kwargs):
            self.settings, self.model, self.task = _settings, model, task
            self.request_metrics = {'requests': 0, 'elapsed_seconds': 0}
        def health_check(self):
            return {'models': list(settings.task_models.values())}
        def chat_json(self, system, user, **kwargs):
            ids = [item['candidate_id'] for item in json.loads(user)['items']]
            calls[self.task].extend(ids)
            if self.task == 'source' and fail['source'] in ids:
                raise LLMServiceError('Simulated source failure')
            answer = source_reply() if self.task == 'source' else {'query': 'organic farming pollution',
                                                                  'openalex_query': 'organic farming pollution'}
            return {'results': [{'candidate_id': identifier, **answer} for identifier in ids]}

    def extract(*args, **kwargs):
        calls['extraction'] += 1
        kwargs['packet_results'].append({'packet_number': 1, 'status': 'completed'})
        return ([{**source_claim(identifier),
                  'source_evidence_span': source_claim()['source_evidence_span'] + (' Repeated measurements were collected.' if identifier == 'two' else ''),
                  'schema_error': None, 'source_page': 1,
                  'source_paper_title': 'Sample', 'source_document_id': 'sample',
                  'source_pdf_sha256': file_hash(pdf)} for identifier in ('one', 'two')], {}, [])

    def external(candidate, client, ranker, configuration, top_k, top_chunks, **kwargs):
        calls['external'].append((candidate['candidate_id'], kwargs.get('resume_record')))
        record = {'candidate_id': candidate['candidate_id'], 'validation_signature': validation_signature(
            candidate, configuration, top_k, top_chunks), 'validation_status': 'external_evidence_not_found',
            'attempts': [{'evidence_documents': [], 'decisions': []}]}
        if fail['external'] == candidate['candidate_id']:
            record['validation_status'] = 'pipeline_error'
            record['attempts'][0]['evidence_documents'] = [{'evidence_document_id': 'retained-paper'}]
            kwargs['on_checkpoint'](record)
            raise LLMServiceError('Simulated external failure')
        return record

    monkeypatch.setattr(module, 'ONTOLOGY_DIR', tmp_path / 'outputs')
    monkeypatch.setattr(module, 'Settings', lambda: settings)
    monkeypatch.setattr(module, 'OpenCodeClient', Client)
    monkeypatch.setattr(module, 'PdfReader', lambda *args: None)
    monkeypatch.setattr(module, 'inspect_pdf', lambda *args: copy.deepcopy(inspection))
    monkeypatch.setattr(module, 'iter_pdf_context_packets', lambda *args, **kwargs: [])
    monkeypatch.setattr(module, 'source_article_identifiers', lambda *args: {})
    monkeypatch.setattr(module, 'pdf_bibliography', lambda *args, **kwargs: [])
    monkeypatch.setattr(module, 'assess_text_quality', lambda *args: {'detected_quality': 'usable'})
    monkeypatch.setattr(module, 'extract_claims', extract)
    monkeypatch.setattr(module, 'SemanticReranker', lambda *args: SimpleNamespace(
        warmup=lambda: 'mock', embedding_cache_hits=0))
    monkeypatch.setattr(module, 'prepare_evidence', lambda *args, **kwargs: {'evidence_documents': []})
    monkeypatch.setattr(module, 'validate_candidate', external)
    monkeypatch.setattr(module, 'assemble_pathways', lambda *args: {'counts':{}})
    return SimpleNamespace(module=module, pdf=pdf, output=output, settings=settings, calls=calls, fail=fail,
                           run=lambda resume=False: module.run_pipeline(pdf, 0, 0, 8, lambda *args: None, claims_per_packet=4, resume=resume))


def test_source_resume_skips_extraction_and_successfully_saved_source_checks(run_fixture):
    case = run_fixture
    case.fail['source'] = 'two'
    with pytest.raises(LLMServiceError, match='source failure'):
        case.run()
    assert [row['candidate_id'] for row in read_jsonl(case.output / 'source_validation.jsonl')] == ['one']
    case.fail['source'] = None
    summary = case.run(resume=True)
    assert case.calls['extraction'] == 1
    assert case.calls['source'] == ['one', 'two', 'two']
    assert summary['source_entailed'] == 2 and summary['resumed']
    before = copy.deepcopy(case.calls)
    case.run(resume=True)
    assert case.calls == before  # Completed external and query checks are reused too.


def test_external_resume_reuses_queries_completed_claims_and_retained_packet(run_fixture):
    case = run_fixture
    case.fail['external'] = 'two'
    with pytest.raises(LLMServiceError, match='external failure'):
        case.run()
    assert case.calls['source'] == ['one', 'two']
    assert case.calls['query'] == ['one', 'two']
    case.fail['external'] = None
    case.run(resume=True)
    assert case.calls['extraction'] == 1
    assert case.calls['source'] == ['one', 'two']
    assert case.calls['query'] == ['one', 'two']
    assert [identifier for identifier, _ in case.calls['external']] == ['one', 'two', 'two']
    retained = case.calls['external'][-1][1]['attempts'][0]['evidence_documents']
    assert retained == [{'evidence_document_id': 'retained-paper'}]


def test_modified_claim_is_rechecked_instead_of_inheriting_an_old_source_verdict(run_fixture):
    case = run_fixture
    case.run()
    rows = read_jsonl(case.output / 'candidates.jsonl')
    rows[0]['source_evidence_span'] = 'Organic farming was introduced to the study area.'
    save_rows(case.output / 'candidates.jsonl', rows)
    summary = case.run(resume=True)
    assert case.calls['source'] == ['one', 'two', 'one']
    assert summary['source_entailed'] == 1
    assert read_jsonl(case.output / 'candidates.jsonl')[0]['source_validation']['decision'] == 'needs_review'


def test_resume_keeps_human_evaluation_labels_for_unchanged_claims(run_fixture):
    case = run_fixture
    case.run()
    rows = read_jsonl(case.output / 'evaluation_sample.jsonl')
    rows[0]['gold']['source_entailed'] = True
    save_rows(case.output / 'evaluation_sample.jsonl', rows)
    case.run(resume=True)
    after = read_jsonl(case.output / 'evaluation_sample.jsonl')
    annotated = next(row for row in after if row['candidate_id'] == rows[0]['candidate_id'])
    assert annotated['gold']['source_entailed'] is True


def test_historical_checkpoint_checks_provenance_without_rewriting_saved_work(run_fixture):
    case = run_fixture
    case.run()
    (case.output / 'pipeline_checkpoint.json').unlink()
    before = {path.name: path.read_bytes() for path in case.output.iterdir() if path.is_file()}
    metadata = resume_metadata(case.output, case.pdf, case.settings, 0, 4)
    assert metadata['imported_extraction']
    assert before == {path.name: path.read_bytes() for path in case.output.iterdir() if path.is_file()}
    (case.output / 'candidates.jsonl').unlink()
    with pytest.raises(ValueError, match='missing'):
        resume_metadata(case.output, case.pdf, case.settings, 0, 4)


@pytest.mark.parametrize('change', ['pdf', 'models', 'pages', 'packet_limit', 'candidate_ids'])
def test_resume_refuses_mismatched_checkpoint_provenance(run_fixture, change):
    case = run_fixture
    case.run()
    settings, pages, limit = case.settings, 0, 4
    if change == 'pdf':
        case.pdf.write_bytes(b'a different PDF')
    elif change == 'models':
        settings = Settings(source_model='opencode-go/mimo-v2.6-flash')
    elif change == 'pages':
        pages = 1
    elif change == 'packet_limit':
        limit = 5
    else:
        rows = read_jsonl(case.output / 'candidates.jsonl')
        rows[0]['candidate_id'] = 'unexpected'
        save_rows(case.output / 'candidates.jsonl', rows)
    with pytest.raises(ValueError):
        resume_metadata(case.output, case.pdf, settings, pages, limit)


def test_resume_api_preserves_saved_findings_and_does_not_start_work_during_test(run_fixture, monkeypatch):
    case = run_fixture
    case.run()
    module = importlib.import_module('backend.api.app')
    monkeypatch.setattr(module, '_jobs', {})
    monkeypatch.setattr(module, '_pdf_path', lambda *args: case.pdf)
    monkeypatch.setattr(module, '_output_dir', lambda *args: case.output)
    monkeypatch.setattr(module.OpenCodeClient, 'health_check', lambda *args: {'models': list(case.settings.task_models.values())})
    thread = Mock()
    monkeypatch.setattr(module.threading, 'Thread', thread)
    (case.output / 'run_log.jsonl').write_text('{"level":"error","message":"previous error"}\n')
    before = (case.output / 'candidates.jsonl').read_bytes()
    response = TestClient(module.app).post('/api/pdfs/test/resume', json={})
    assert response.status_code == 202 and response.json()['resume']
    assert response.json()['logs'][0]['message'] == 'previous error'
    assert (case.output / 'candidates.jsonl').read_bytes() == before
    assert (case.output / 'validation_runs.jsonl').exists()
    thread.return_value.start.assert_called_once()


def test_resume_rejects_switching_extraction_mode_or_target_size(run_fixture):
    case = run_fixture
    case.run()
    with pytest.raises(ValueError, match='original extraction mode'):
        resume_metadata(case.output, case.pdf, case.settings, 0, 4, extraction_mode='whole_packet')
    with pytest.raises(ValueError, match='original extraction target size'):
        resume_metadata(case.output, case.pdf, case.settings, 0, 4,
                        extraction_mode='target_context', target_words=1200)


def test_pipeline_persists_failed_and_pending_targets_without_completing_checkpoint(run_fixture, monkeypatch):
    case = run_fixture
    def fail(*args, **kwargs):
        kwargs['packet_results'].extend([{'status': 'service_error', 'target_id': 'failed'},
                                         {'status': 'not_started', 'target_id': 'pending'}])
        raise LLMServiceError('Provider stopped during extraction')
    monkeypatch.setattr(case.module, 'extract_claims', fail)
    with pytest.raises(LLMServiceError):
        case.run()
    assert [r['status'] for r in read_jsonl(case.output / 'extraction_packets.jsonl')] == ['service_error', 'not_started']
    checkpoint = json.loads((case.output / 'pipeline_checkpoint.json').read_text(encoding='utf-8'))
    assert not checkpoint['extraction_complete'] and checkpoint['extraction_mode'] == 'target_context'


def test_global_assembly_runs_after_external_validation_with_its_assigned_mimo_flash(run_fixture, monkeypatch):
    case = run_fixture
    def assembly(root, client, settings, emit):
        assert root == case.output.parent
        assert client.task == 'pathway_assembly'
        assert client.model == 'opencode-go/mimo-v2.6-flash'
        assert len(read_jsonl(case.output / 'validation_runs.jsonl')) == 2
        assert len(case.calls['external']) == 2
        return {'counts':{'claims':2, 'entities':3}}
    monkeypatch.setattr(case.module, 'assemble_pathways', assembly)
    assert case.run()['pathway_assembly']['claims'] == 2


def test_assembly_failure_resumes_without_repeating_extraction_or_validation(run_fixture, monkeypatch):
    case = run_fixture
    def failed(*args):
        raise LLMServiceError('MiMo assembly provider error')
    monkeypatch.setattr(case.module, 'assemble_pathways', failed)
    with pytest.raises(LLMServiceError, match='assembly'):
        case.run()
    completed = copy.deepcopy(case.calls)
    monkeypatch.setattr(case.module, 'assemble_pathways', lambda *args:{'counts':{}})
    case.run(resume=True)
    assert case.calls == completed


def test_resume_rechecks_external_thinking_change_without_repeating_extraction(run_fixture,monkeypatch):
    from dataclasses import replace
    case=run_fixture;case.run()
    monkeypatch.setattr(case.module,'Settings',lambda:replace(case.settings,thinking={'external':True}))
    case.run(resume=True)
    assert case.calls['extraction']==1
    assert case.calls['source']==['one','two']
    assert case.calls['query']==['one','two']
    assert [c for c,_ in case.calls['external']]==['one','two','one','two']


def test_resume_rejects_changed_extraction_thinking(run_fixture,monkeypatch):
    from dataclasses import replace
    case=run_fixture;case.run()
    monkeypatch.setattr(case.module,'Settings',lambda:replace(case.settings,thinking={'extraction':True}))
    with pytest.raises(ValueError,match='original extraction Thinking'):
        case.run(resume=True)


def test_legacy_extraction_without_thinking_metadata_is_treated_as_on(run_fixture,monkeypatch):
    from dataclasses import replace
    case=run_fixture;case.run()
    (case.output/'pipeline_checkpoint.json').unlink()
    summary=json.loads((case.output/'summary.json').read_text())
    summary.pop('thinking',None)
    (case.output/'summary.json').write_text(json.dumps(summary))
    monkeypatch.setattr(case.module,'Settings',lambda:replace(case.settings,thinking={'extraction':False}))
    with pytest.raises(ValueError,match='original extraction Thinking'):
        case.run(resume=True)
