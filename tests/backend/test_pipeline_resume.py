"""Recover failed stages without repeating extraction or successful judgements."""
import copy
import importlib
import json
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.config.settings import SCHEMA_VERSION
from backend.config.settings import Settings
from backend.pipelines.checkpoints import resume_metadata, save_checkpoint
from backend.providers.llm import LLMServiceError
from backend.shared.artifacts import save_rows
from backend.shared.storage import file_hash, read_jsonl
from archive.legacy_pipeline.research_historical.external_validation.workflow import validation_signature
from tests.regression.test_pipeline_v24 import source_claim, source_reply


@pytest.fixture
def run_fixture(tmp_path, monkeypatch):
    module = importlib.import_module('backend.pipelines.document')
    pdf = tmp_path / 'sample.pdf'
    pdf.write_bytes(b'PDF fixture; deterministic parsing is supplied below')
    settings = Settings(validation_batch_size=1,source_workers=1)
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
            items = json.loads(user)['items']
            keyed = bool(items and 'item_id' in items[0])
            ids = kwargs['request_label'].split(': ', 1)[1].split(', ') if keyed else [item['candidate_id'] for item in items]
            calls[self.task].extend(ids)
            if self.task == 'source' and fail['source'] in ids:
                raise LLMServiceError('Simulated source failure')
            answer = source_reply() if self.task == 'source' else {'query': 'organic farming pollution',
                                                                  'openalex_query': 'organic farming pollution'}
            return {'results': {item['item_id']: copy.deepcopy(answer) for item in items}} if keyed else {
                'results': [{'candidate_id': identifier, **answer} for identifier in ids]}

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
    def assemble(root, client, settings, emit, **kwargs):
        from backend.stages.pathway_assembly.corpus import collect_corpus
        edges,endpoints=collect_corpus(root,provisional=True)
        labels=sorted({e[role] for e in edges for role in ('subject','object')})
        nodes=[{'id':label,'label':label,'type':'Unresolved','endpoint_ids':[]} for label in labels]
        return {'graph':{'nodes':nodes,'edges':[{**e,'source':e['subject'],'target':e['object']} for e in edges]},
                'counts':{'claims':len(edges),'entities':len(nodes),'papers':1},'endpoints':endpoints}
    monkeypatch.setattr(module, 'assemble_pathways', assemble)
    return SimpleNamespace(module=module, pdf=pdf, output=output, settings=settings, calls=calls, fail=fail,
                           run=lambda resume=False: module.run_pipeline(pdf, 0, 8, lambda *args: None, claims_per_packet=4, resume=resume))


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


def test_active_source_flow_does_not_create_retired_semantic_votes(run_fixture):
    case = run_fixture
    summary = case.run()
    candidates = read_jsonl(case.output / 'candidates.jsonl')
    assert summary['schema_valid'] == 2 and summary['source_entailed'] == 2
    assert summary['semantic_stage'] == 'removed'
    assert all('semantic_validation' not in candidate for candidate in candidates)
    assert not (case.output / 'semantic_validation.jsonl').exists()
    assert not (case.output / 'semantic_valid_candidates.jsonl').exists()
    graph = json.loads((case.output / 'source_graph.json').read_text(encoding='utf-8'))
    assert len(graph['edges']) == 2


def test_external_failure_resumes_without_repeating_extraction_and_source(run_fixture,monkeypatch):
    case=run_fixture
    original=case.module.run_pathway_assessment
    monkeypatch.setattr(case.module,'run_pathway_assessment',Mock(side_effect=LLMServiceError('Simulated pathway failure')))
    with pytest.raises(LLMServiceError,match='pathway failure'):
        case.run()
    before=copy.deepcopy(case.calls)
    monkeypatch.setattr(case.module,'run_pathway_assessment',original)
    summary=case.run(resume=True)
    assert case.calls==before
    assert summary['externally_evaluated']==0
    assert summary['pathway_validation']['individual_external_calls']==0


def test_modified_claim_is_rechecked_instead_of_inheriting_an_old_source_verdict(run_fixture):
    case = run_fixture
    case.run()
    rows = read_jsonl(case.output / 'candidates.jsonl')
    rows[0]['source_evidence_span'] = 'Organic farming was introduced to the study area.'
    save_rows(case.output / 'candidates.jsonl', rows)
    summary = case.run(resume=True)
    assert case.calls['source'] == ['one', 'two', 'one']  # One initial check of the changed claim; no reassessment.
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


def test_synthesis_model_change_reuses_extraction_but_invalidates_external_assessments(run_fixture):
    from backend.stages.neighbor_validation.workflow import config_signature
    case=run_fixture
    case.run()
    metadata=json.loads((case.output/'pipeline_checkpoint.json').read_text(encoding='utf-8'))
    metadata['task_models']['external']='opencode-go/mimo-v2.6-pro'
    save_checkpoint(case.output,metadata)
    before=(case.output/'candidates.jsonl').read_bytes()
    restored=resume_metadata(case.output,case.pdf,case.settings,0,4)
    assert restored['extraction_complete']
    assert (case.output/'candidates.jsonl').read_bytes()==before
    target={'input_signature':'unchanged-source-and-graph'}
    assert config_signature(target,case.settings)!=config_signature(target,replace(case.settings,external_model='opencode-go/mimo-v2.6-pro'))


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
    assert (case.output / 'pipeline_checkpoint.json').exists()
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


def test_global_assembly_runs_before_pathway_validation_with_mimo_flash(run_fixture, monkeypatch):
    case=run_fixture
    original=case.module.assemble_pathways
    def assembly(root,client,settings,emit,**kwargs):
        assert root==case.output.parent and client.task=='pathway_assembly'
        assert client.model=='opencode-go/mimo-v2.6-flash'
        assert kwargs['provisional'] is True
        assert not case.calls['external'] and not case.calls['query']
        return original(root,client,settings,emit,**kwargs)
    monkeypatch.setattr(case.module,'assemble_pathways',assembly)
    summary=case.run()
    assert summary['pathway_assembly']['claims']==2
    assert summary['knowledge_building_mode']=='pathway_only'


def test_assembly_failure_resumes_without_repeating_extraction_or_validation(run_fixture, monkeypatch):
    case = run_fixture
    def failed(*args,**kwargs):
        raise LLMServiceError('MiMo assembly provider error')
    monkeypatch.setattr(case.module, 'assemble_pathways', failed)
    with pytest.raises(LLMServiceError, match='assembly'):
        case.run()
    completed = copy.deepcopy(case.calls)
    monkeypatch.setattr(case.module, 'assemble_pathways', lambda *args,**kwargs:{'graph':{'nodes':[],'edges':[]},'counts':{}})
    case.run(resume=True)
    assert case.calls == completed


def test_resume_rechecks_external_thinking_change_without_repeating_extraction(run_fixture,monkeypatch):
    from dataclasses import replace
    case=run_fixture;case.run()
    monkeypatch.setattr(case.module,'Settings',lambda:replace(case.settings,thinking={'external':True}))
    case.run(resume=True)
    assert case.calls['extraction']==1
    assert case.calls['source']==['one','two']
    assert case.calls['query']==[] and case.calls['external']==[]  # standalone claims are never externally assessed


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


def test_source_scope_policy_refresh_reuses_extraction_and_preserves_previous_reading(run_fixture):
    from backend.stages.source_validation.abbreviations import SOURCE_VALIDATION_POLICY
    case = run_fixture
    case.run()
    rows = read_jsonl(case.output / 'source_validation.jsonl')
    for row in rows:
        row['validation_policy'] = 'old-fidelity-only-policy'
        row.pop('project_scope')
    save_rows(case.output / 'source_validation.jsonl', rows)
    summary = case.run(resume=True)
    assert case.calls['extraction'] == 1
    assert len(case.calls['source']) == 4
    assert summary['pathway_eligible_claims'] == 2
    for row in read_jsonl(case.output / 'source_validation.jsonl'):
        assert row['validation_policy'] == SOURCE_VALIDATION_POLICY
        assert row['project_scope']['decision'] == 'in_scope'
        assert row['previous_assessment']['validation_policy'] == 'old-fidelity-only-policy'
    case.run(resume=True)
    assert len(case.calls['source']) == 4


def test_external_redo_and_its_resume_keep_extraction_source_and_manual_records(run_fixture, monkeypatch):
    from backend.stages.source_validation.review import apply_manual_source_review, source_review_state
    case = run_fixture
    case.run()
    candidates = read_jsonl(case.output / 'candidates.jsonl')
    apply_manual_source_review(candidates[0], 'project_scope', 'accept',
        expected_revision=source_review_state(candidates[0])['revision'], note='Researcher-reviewed source context.')
    save_rows(case.output / 'candidates.jsonl', candidates)
    save_rows(case.output / 'source_validation.jsonl', [c['source_validation'] for c in candidates])
    original = case.module.run_pathway_assessment
    calls = []
    def external(*args, **kwargs):
        calls.append(kwargs.get('assessment_id'))
        return original(*args, **kwargs)
    monkeypatch.setattr(case.module, 'run_pathway_assessment', external)
    before = (case.output / 'candidates.jsonl').read_bytes()
    summary = case.module.run_pipeline(case.pdf, 0, 8, lambda *args: None,
        claims_per_packet=4, resume=True, external_assessment_id='redo-test')
    assert summary['external_assessment_id'] == 'redo-test'
    assert json.loads((case.output / 'pipeline_checkpoint.json').read_text())['external_assessment_id'] == 'redo-test'
    case.run(resume=True)
    assert calls == ['redo-test', 'redo-test']
    assert case.calls['extraction'] == 1 and len(case.calls['source']) == 2
    assert (case.output / 'candidates.jsonl').read_bytes() == before


def test_clear_scope_exclusions_skip_assembly_without_falsifying_pdf_fidelity(run_fixture, monkeypatch):
    case = run_fixture
    original_reply = source_reply
    def excluded_reply():
        return {**original_reply(), 'project_scope': {
            'decision': 'out_of_scope', 'rationale': 'The supplied assertion is outside the project mechanism.'}}
    monkeypatch.setitem(globals(), 'source_reply', excluded_reply)
    summary = case.run()
    assert summary['source_entailed'] == 2
    assert summary['source_project_scope']['out_of_scope'] == 2
    assert summary['pathway_eligible_claims'] == 0
    assert summary['pathway_assembly']['claims'] == 0
    assert len(case.calls['source']) == 2  # No redundant reread of a clear scope exclusion.
    assert len(read_jsonl(case.output / 'candidates.jsonl')) == 2


def test_uncertain_scope_stays_for_manual_review_without_another_model_reading(run_fixture, monkeypatch):
    case = run_fixture
    original_reply = source_reply
    reads = []
    def scope_reply():
        reads.append(True)
        answer = original_reply()
        if len(reads) <= 2:
            answer['project_scope'] = {'decision': 'needs_review', 'rationale': 'The local domain context is ambiguous.'}
        return answer
    monkeypatch.setitem(globals(), 'source_reply', scope_reply)
    summary = case.run()
    assert case.calls['extraction'] == 1 and len(case.calls['source']) == 2
    assert summary['pathway_eligible_claims'] == 0
    assert summary['source_project_scope']['needs_review'] == 2
    for row in read_jsonl(case.output / 'source_validation.jsonl'):
        assert row['project_scope']['decision'] == 'needs_review'
        assert 'source_review_audit' not in row
    case.run(resume=True)
    assert len(case.calls['source']) == 2


def test_resume_preserves_manual_source_and_scope_acceptances_without_model_reassessment(run_fixture, monkeypatch):
    from backend.stages.source_validation.review import apply_manual_source_review, source_review_state
    case = run_fixture
    original_reply = source_reply
    monkeypatch.setitem(globals(), 'source_reply', lambda: {**original_reply(), 'decision': 'needs_review',
        'project_scope': {'decision': 'needs_review', 'rationale': 'Manual reading is needed.'}})
    case.run()
    candidates = read_jsonl(case.output / 'candidates.jsonl')
    for candidate in candidates:
        for dimension in ('source_fidelity', 'project_scope'):
            apply_manual_source_review(candidate, dimension, 'accept', expected_revision=source_review_state(candidate)['revision'])
    save_rows(case.output / 'candidates.jsonl', candidates)
    save_rows(case.output / 'source_validation.jsonl', [c['source_validation'] for c in candidates])
    summary = case.run(resume=True)
    assert summary['source_entailed'] == summary['pathway_eligible_claims'] == 2
    assert summary['pathway_assembly']['claims'] == 2
    assert case.calls['extraction'] == 1 and len(case.calls['source']) == 2
    assert all('manual_reviews' in c['source_validation'] for c in read_jsonl(case.output / 'candidates.jsonl'))
