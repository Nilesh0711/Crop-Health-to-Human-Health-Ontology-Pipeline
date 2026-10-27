"""Claim-local repair, immutable history, source gates and external recovery."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import importlib
import json

import pytest

from backend.pipelines.claim_repair import run_claim_action, refresh_saved_views
from backend.shared.artifacts import save_rows
from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.shared.storage import file_hash, read_jsonl
from backend.stages.external_validation.workflow import validate_candidate


class FakeClient:
    def __init__(self, settings, model=None, **kwargs):
        self.settings, self.model = settings, model

    def health_check(self):
        return {'models': list(Settings().task_models.values())}


@pytest.fixture(autouse=True)
def isolate_pathway_assembly(monkeypatch):
    monkeypatch.setattr('backend.pipelines.claim_repair.assemble_pathways', lambda *args: {'counts':{}})


@pytest.fixture
def saved_claim(tmp_path):
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'PDF fixture whose parser is mocked')
    output = tmp_path / 'output'
    output.mkdir()
    claim = {'candidate_id': 'target', 'subject': 'lead', 'relation': 'increases',
             'object': 'microbial activity', 'subject_type': 'Contaminant', 'object_type': 'SoilBiota',
             'certainty': 'descriptive', 'qualifiers': {}, 'schema_error': None,
             'source_page': 1, 'source_page_end': 1, 'source_packet_number': 1,
             'source_pdf_sha256': file_hash(pdf), 'source_evidence_span': 'Lead reduced microbial activity.',
             'source_context_packet': 'Lead reduced microbial activity.',
             'semantic_validation': {'candidate_id': 'target', 'decision': 'accepted'},
             'source_validation': {'candidate_id': 'target', 'decision': 'unsupported', 'reasons': ['Wrong direction']}}
    unrelated = {**deepcopy(claim), 'candidate_id': 'unrelated'}
    save_rows(output / 'candidates.jsonl', [claim, unrelated])
    save_rows(output / 'validation_runs.jsonl', [{'candidate_id': 'unrelated', 'validation_status': 'external_evidence_not_found', 'attempts': []}])
    return pdf, output, claim, unrelated


def setup_repair(claim):
    proposed = deepcopy(claim)
    proposed.update(candidate_id='new-assertion', relation='reduces')
    proposed.pop('source_validation')
    proposed.pop('semantic_validation')
    packet = {'packet_number': 1, 'sentences': [{'text': claim['source_evidence_span']}]}
    return proposed, packet


@pytest.mark.parametrize('source_decision', ['entailed', 'needs_review', 'unsupported'])
@pytest.mark.parametrize('saved_context', [False, True])
def test_repair_rechecks_only_target_and_external_requires_source_success(saved_claim, source_decision, saved_context):
    pdf, output, original, unrelated = saved_claim
    proposed, packet = setup_repair(original)
    if saved_context:
        save_rows(output / 'extraction_contexts.jsonl', [packet])
    module = importlib.import_module('backend.pipelines.claim_repair')
    settings = replace(Settings(), enable_nli=False)
    with patch.object(module, 'Settings', return_value=settings), \
            patch.object(module, 'OpenCodeClient', FakeClient), \
            patch.object(module, 'iter_pdf_context_packets', return_value=iter([packet])) as parser, \
            patch.object(module, 'extract_claims', return_value=([proposed], {}, [])) as extract, \
            patch.object(module, 'validate_source_claim', return_value={'candidate_id': 'target', 'decision': source_decision}) as source, \
            patch.object(module, 'SemanticReranker'), \
            patch.object(module, 'translate_query', return_value={'query': 'neutral query'}) as query, \
            patch.object(module, 'validate_candidate', return_value={'candidate_id': 'target', 'validation_status': 'external_evidence_not_found', 'attempts': []}) as external:
        run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    assert extract.call_count == source.call_count == 1
    if saved_context:
        parser.assert_not_called()
    assert extract.call_args.kwargs['repair_target']['source_failure']['reasons'] == ['Wrong direction']
    assert external.call_count == query.call_count == int(source_decision == 'entailed')
    candidates = read_jsonl(output / 'candidates.jsonl')
    assert candidates[1] == unrelated
    assert candidates[0]['candidate_id'] == 'target'
    assert candidates[0]['assertion_id'] == 'new-assertion'
    assert candidates[0]['relation'] == 'reduces'
    assert candidates[0]['repair_revision'] == 1
    history = read_jsonl(output / 'claim_revisions.jsonl')[0]
    assert history['original_candidate'] == original
    assert history['status'] == ('completed' if source_decision == 'entailed' else 'still_needs_review')
    assert (output / 'audit_graph.json').exists()
    assert next(r for r in read_jsonl(output / 'validation_runs.jsonl') if r['candidate_id'] == 'unrelated')['validation_status'] == 'external_evidence_not_found'


def test_changed_pdf_stops_before_any_model_call(saved_claim):
    pdf, output, *_ = saved_claim
    pdf.write_bytes(b'changed source')
    with patch('backend.pipelines.claim_repair.OpenCodeClient') as client:
        with pytest.raises(ValueError, match='identity changed'):
            run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    client.assert_not_called()


def test_partial_summary_counts_a_source_entailed_claim_without_external_record(saved_claim):
    pdf, output, original, unrelated = saved_claim
    original['source_validation']['decision'] = 'entailed'
    save_rows(output / 'candidates.jsonl', [original, unrelated])
    summary = refresh_saved_views(pdf, output)
    assert summary['external_incomplete'] == 1
    assert summary['run_status'] == 'completed_with_warnings'


def test_repair_model_failure_preserves_original_and_audits_error(saved_claim):
    pdf, output, original, _ = saved_claim
    _, packet = setup_repair(original)
    with patch('backend.pipelines.claim_repair.OpenCodeClient', FakeClient), \
            patch('backend.pipelines.claim_repair.iter_pdf_context_packets', return_value=iter([packet])), \
            patch('backend.pipelines.claim_repair.extract_claims', side_effect=LLMServiceError('HTTP 429')):
        with pytest.raises(LLMServiceError, match='HTTP 429'):
            run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    assert read_jsonl(output / 'candidates.jsonl')[0] == original
    assert read_jsonl(output / 'claim_revisions.jsonl')[0]['status'] == 'failed'


def test_no_grounded_correction_does_not_replace_original(saved_claim):
    pdf, output, original, _ = saved_claim
    _, packet = setup_repair(original)
    with patch('backend.pipelines.claim_repair.OpenCodeClient', FakeClient), \
            patch('backend.pipelines.claim_repair.iter_pdf_context_packets', return_value=iter([packet])), \
            patch('backend.pipelines.claim_repair.extract_claims', return_value=([], {}, [])):
        run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    assert read_jsonl(output / 'candidates.jsonl')[0] == original
    assert read_jsonl(output / 'claim_revisions.jsonl')[0]['status'] == 'no_grounded_correction'


def test_removed_semantic_stage_does_not_run_during_repair(saved_claim):
    pdf, output, original, _ = saved_claim
    proposed, packet = setup_repair(original)
    with patch('backend.pipelines.claim_repair.OpenCodeClient', FakeClient), \
            patch('backend.pipelines.claim_repair.iter_pdf_context_packets', return_value=iter([packet])), \
            patch('backend.pipelines.claim_repair.extract_claims', return_value=([proposed], {}, [])), \
            patch('backend.pipelines.claim_repair.validate_source_claim', return_value={'candidate_id': 'target', 'decision': 'unsupported'}) as source, \
            patch('backend.pipelines.claim_repair.validate_candidate') as external:
        run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    source.assert_called_once()
    external.assert_not_called()
    assert read_jsonl(output / 'candidates.jsonl')[0]['semantic_validation']['decision'] == 'not_run'


def test_repair_archives_gold_for_changed_assertion_and_preserves_other_gold(saved_claim):
    from backend.stages.evaluation.metrics import evaluation_sample
    pdf, output, original, unrelated = saved_claim
    rows = evaluation_sample([original, unrelated], [], 80)
    for row in rows:
        row['gold']['reviewer'] = 'human-reviewer'
        row['gold']['valid_triple'] = False
    save_rows(output / 'evaluation_sample.jsonl', rows)
    proposed, packet = setup_repair(original)
    with patch('backend.pipelines.claim_repair.OpenCodeClient', FakeClient), \
            patch('backend.pipelines.claim_repair.iter_pdf_context_packets', return_value=iter([packet])), \
            patch('backend.pipelines.claim_repair.extract_claims', return_value=([proposed], {}, [])):
        run_claim_action(pdf, output, 'target', 'repair', 3, lambda *_: None)
    after = {r['candidate_id']: r for r in read_jsonl(output / 'evaluation_sample.jsonl')}
    assert after['unrelated']['gold']['reviewer'] == 'human-reviewer'
    assert after['target']['gold']['reviewer'] is None
    assert read_jsonl(output / 'claim_revisions.jsonl')[0]['original_evaluation']['gold']['reviewer'] == 'human-reviewer'


def test_joint_timeout_preserves_entire_packet_for_one_manual_retry():
    claim = {'candidate_id': 'one', 'subject': 'A', 'object': 'B', 'relation': 'affects'}
    documents = [{'evidence_document_id': i, 'evidence_level': 'independent_external',
                  'independence_group': i, 'evidence_chunks': [{'text': 'Evidence.'}]} for i in ['first', 'second']]
    client = SimpleNamespace(model=Settings().external_model)
    checkpoints = []
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': documents}), \
            patch('backend.stages.external_validation.workflow.judge_evidence_pack', side_effect=LLMServiceError('timed out')):
        with pytest.raises(LLMServiceError):
            validate_candidate(claim, client, None, Settings(external_judgement_mode='joint'), on_checkpoint=checkpoints.append)
    saved = checkpoints[-1]
    assert saved['validation_status'] == 'pipeline_error'
    assert saved['attempts'][0]['evidence_documents'] == documents
    assert saved['attempts'][0]['decisions'] == []
    decisions = [{'evidence_document_id': d['evidence_document_id'], 'decision': 'insufficient'} for d in documents]
    with patch('backend.stages.external_validation.workflow.retrieve_round') as retrieve, \
            patch('backend.stages.external_validation.workflow.judge_evidence_pack', return_value=(decisions, {})) as judge:
        result = validate_candidate(claim, client, None, Settings(external_judgement_mode='joint'), resume_record=saved)
    retrieve.assert_not_called()
    judge.assert_called_once()
    assert len(result['attempts'][0]['decisions']) == 2


def test_external_timeout_is_separate_from_scope_and_extraction():
    settings = replace(Settings(), request_timeout_seconds=300, external_timeout_seconds=600,
                       extraction_timeout_seconds=420)
    for task, expected in [('query', 300), ('source', 300), ('extraction', 420), ('external', 600)]:
        client = OpenCodeClient(settings, settings.task_models[task], task=task)
        assert client.settings.request_timeout_seconds == expected


def test_claim_action_api_is_busy_safe_and_never_clears_saved_claims(saved_claim):
    pdf, output, original, unrelated = saved_claim
    module = importlib.import_module('backend.api.app')
    jobs = {}
    with patch.object(module, '_pdf_path', return_value=pdf), \
            patch.object(module, '_output_dir', return_value=output), \
            patch.object(module, '_jobs', jobs), patch.object(module.threading, 'Thread'):
        result = module.start_claim_action('pdf', 'target', 'repair', module.RunOptions())
        assert result['action'] == 'repair'
        assert read_jsonl(output / 'candidates.jsonl') == [original, unrelated]
        with pytest.raises(module.HTTPException) as error:
            module.start_claim_action('pdf', 'target', 'repair', module.RunOptions())
        assert error.value.status_code == 409
