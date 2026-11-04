"""Human decisions are independent, auditable, version-bound, and inference-free."""
from copy import deepcopy
import importlib

from fastapi.testclient import TestClient
import pytest

from backend.shared.artifacts import save_rows, save_json
from backend.shared.storage import read_jsonl
from backend.stages.source_validation.review import (
    apply_manual_source_review, source_review_state, source_fidelity_decision,
)
from backend.stages.source_validation.scope import source_pathway_eligible, source_scope_decision
from backend.stages.pathway_assembly.corpus import collect_corpus
from tests.backend.test_source_project_scope import example


def claim():
    candidate, model = example()
    model['decision'] = 'needs_review'
    model['project_scope']['decision'] = 'needs_review'
    candidate['source_validation'] = model
    return candidate


def decide(candidate, dimension, decision):
    return apply_manual_source_review(candidate, dimension, decision,
        expected_revision=source_review_state(candidate)['revision'], note='Read the original source and its context.')


def test_manual_acceptances_are_separate_and_never_overwrite_model_assessments(tmp_path):
    candidate = claim()
    original = deepcopy(candidate)
    event = decide(candidate, 'source_fidelity', 'accept')
    assert source_fidelity_decision(candidate) == 'entailed'
    assert source_scope_decision(candidate) == 'needs_review'
    assert not source_pathway_eligible(candidate)
    decide(candidate, 'project_scope', 'accept')
    assert source_pathway_eligible(candidate)
    assert event['model_assessment'] == original['source_validation']
    assert {k: v for k, v in candidate['source_validation'].items() if k != 'manual_reviews'} == original['source_validation']
    assert {k: v for k, v in candidate.items() if k != 'source_validation'} == {k: v for k, v in original.items() if k != 'source_validation'}
    save_rows(tmp_path / 'Document A' / 'candidates.jsonl', [candidate])
    assert len(collect_corpus(tmp_path, provisional=True)[0]) == 1


@pytest.mark.parametrize('dimension', ['source_fidelity', 'project_scope'])
def test_either_rejection_removes_claim_from_provisional_assembly(tmp_path, dimension):
    candidate = claim()
    decide(candidate, 'source_fidelity', 'accept')
    decide(candidate, 'project_scope', 'accept')
    decide(candidate, dimension, 'reject')
    assert not source_pathway_eligible(candidate)
    save_rows(tmp_path / 'Document A' / 'candidates.jsonl', [candidate])
    assert collect_corpus(tmp_path, provisional=True) == ([], [])


@pytest.mark.parametrize('change', ['claim', 'evidence', 'model_assessment'])
def test_changed_inputs_invalidate_manual_decisions(change):
    candidate = claim()
    decide(candidate, 'source_fidelity', 'accept')
    decide(candidate, 'project_scope', 'accept')
    if change == 'claim': candidate['qualifiers']['condition'] = 'Condition Q'
    elif change == 'evidence': candidate['source_evidence_span'] += ' Additional source context.'
    else: candidate['source_validation']['rationale'] = 'A new model reading.'
    state = source_review_state(candidate)
    assert set(state['stale_dimensions']) == {'source_fidelity', 'project_scope'}
    assert not state['active_reviews'] and not source_pathway_eligible(candidate)


def test_stale_browser_cannot_overwrite_a_newer_human_decision():
    candidate = claim()
    old_revision = source_review_state(candidate)['revision']
    decide(candidate, 'source_fidelity', 'accept')
    with pytest.raises(ValueError, match='Refresh'):
        apply_manual_source_review(candidate, 'source_fidelity', 'reject', expected_revision=old_revision)


@pytest.mark.parametrize('invalid', ['schema', 'evidence', 'sentence_id', 'technical_failure'])
def test_acceptance_cannot_bypass_structural_or_technical_failures(invalid):
    candidate = claim()
    if invalid == 'schema': candidate['schema_error'] = 'Invalid structure'
    elif invalid == 'evidence': candidate['source_evidence_span'] = ''
    elif invalid == 'sentence_id': candidate['source_evidence_sentence_numbers'] = [False]
    else: candidate['source_validation']['batch_error'] = True
    with pytest.raises(ValueError, match='valid claim structure'):
        decide(candidate, 'source_fidelity', 'accept')
    # A researcher can still explicitly exclude the unusable record.
    decide(candidate, 'source_fidelity', 'reject')
    assert source_fidelity_decision(candidate) == 'unsupported'


@pytest.fixture
def api(tmp_path, monkeypatch):
    module = importlib.import_module('backend.api.app')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'No PDF processing is requested')
    root = tmp_path / 'ontology'
    output = root / 'Document A'
    candidate = claim()
    save_rows(output / 'candidates.jsonl', [candidate])
    save_rows(output / 'source_validation.jsonl', [candidate['source_validation']])
    save_json(output / 'external_validation_target.json', {'target': 'whole_pathway'})
    monkeypatch.setattr(module, '_jobs', {})
    monkeypatch.setattr(module, '_pdf_map', lambda: {'paper-id': pdf})
    monkeypatch.setattr(module, '_output_dir', lambda _: output)
    monkeypatch.setattr(module, 'ONTOLOGY_DIR', root)
    def no_inference(*args, **kwargs):
        raise AssertionError('Manual review must not instantiate a model client')
    monkeypatch.setattr('backend.pipelines.claim_repair.OpenCodeClient', no_inference)
    return module, output, TestClient(module.app)


def post_review(client, dimension='source_fidelity', decision='accept', revision=None):
    if revision is None:
        revision = client.get('/api/pdfs/paper-id/claims').json()['claims'][0]['source_review']['revision']
    return client.post('/api/pdfs/paper-id/claims/claim-a/source-review', json={
        'dimension': dimension, 'decision': decision, 'expected_revision': revision, 'note': 'Checked against the original PDF.'})


def test_api_persists_both_decisions_history_and_views_without_llm_calls(api):
    module, output, client = api
    original = deepcopy(read_jsonl(output / 'candidates.jsonl')[0])
    first = post_review(client)
    assert first.status_code == 200 and first.json()['resume_required']
    assert first.json()['summary']['source_entailed'] == 1
    assert first.json()['summary']['pathway_eligible_claims'] == 0
    second = post_review(client, 'project_scope')
    assert second.status_code == 200
    assert second.json()['summary']['pathway_eligible_claims'] == 1
    saved = read_jsonl(output / 'candidates.jsonl')[0]
    assert source_pathway_eligible(saved)
    assert {k: v for k, v in saved['source_validation'].items() if k != 'manual_reviews'} == original['source_validation']
    assert read_jsonl(output / 'source_validation.jsonl')[0] == saved['source_validation']
    assert len(read_jsonl(output / 'source_review_history.jsonl')) == 2
    rows = client.get('/api/pdfs/paper-id/claims').json()['claims']
    assert len(rows[0]['source_review_history']) == 2
    assert len(rows[0]['source_review']['active_reviews']) == 2
    assert len(read_jsonl(output / 'run_log.jsonl')) == 2
    assert not (output / 'llm_requests.jsonl').exists()
    assert not module._jobs


def test_api_rejects_stale_versions_running_jobs_and_invalid_requests(api):
    module, output, client = api
    old = client.get('/api/pdfs/paper-id/claims').json()['claims'][0]['source_review']['revision']
    assert post_review(client, revision=old).status_code == 200
    assert post_review(client, decision='reject', revision=old).status_code == 409
    module._jobs['active'] = {'status': 'running'}
    assert post_review(client).status_code == 409
    module._jobs.clear()
    assert post_review(client, decision='invented').status_code == 422
    assert client.post('/api/pdfs/paper-id/claims/missing/source-review', json={
        'dimension': 'source_fidelity', 'decision': 'reject', 'expected_revision': '0' * 64}).status_code == 404
