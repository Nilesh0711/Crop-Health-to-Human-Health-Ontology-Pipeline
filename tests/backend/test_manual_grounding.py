"""Manual grounding queues saved extraction, never starts a new extraction."""
import json
from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.runs import create_router
from backend.shared.artifacts import save_rows, save_json
from backend.stages.claim_extraction.contract import (
    CLOSED_EXTRACTION_CONTRACT_VERSION, CLOSED_PREDICATES, CLOSED_PREDICATE_SET_VERSION, MEASURED_OBSERVATION_CONTRACT_VERSION,
    closed_predicate_whitelist_digest,
)


def closed_claim(candidate_id='c1', **changes):
    entry = CLOSED_PREDICATES['P7']
    return {'candidate_id': candidate_id, 'subject': 'root system', 'relation': 'has part', 'object': 'root',
            'predicate_alias': 'P7', 'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
            'proposed_relation_iri': entry['iri'], 'proposed_relation_id': entry['id'],
            'proposed_relation_mapping': 'exact', 'extraction_review_status': 'not_flagged',
            'extraction_contract_version': CLOSED_EXTRACTION_CONTRACT_VERSION, 'source_pdf_sha256': 'offline-pdf', **changes}


def saved_extraction(output, version=CLOSED_EXTRACTION_CONTRACT_VERSION):
    save_json(output / 'pipeline_checkpoint.json', {'extraction_complete': True, 'pdf_sha256': 'offline-pdf',
              'extraction_contract_version': version,
              'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
              'predicate_whitelist_sha256': closed_predicate_whitelist_digest()})
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'completed'}])


def setup(tmp_path, monkeypatch):
    from backend.api.routes import runs
    monkeypatch.setattr(runs, 'MANUAL_GROUNDING_ENABLED', True)
    output = tmp_path / 'paper'; output.mkdir()
    saved_extraction(output)
    save_rows(output / 'candidates.jsonl', [closed_claim()])
    launches = []
    class Thread:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def start(self): launches.append(self.kwargs)
    monkeypatch.setattr(runs.threading, 'Thread', Thread)
    workspace = SimpleNamespace(_lock=RLock(), _jobs={}, _pdf_path=lambda _: tmp_path / 'paper.pdf',
        _output_dir=lambda _: output, _read_json=lambda path, default: json.loads(path.read_text()) if path.exists() else default,
        _persist_job=lambda _: None, _job_snapshot=lambda job: dict(job), _worker=lambda *args: None)
    app = FastAPI(); app.include_router(create_router(workspace)[0])
    return TestClient(app), output, workspace, launches


def test_manual_endpoint_queues_grounding_only_and_preserves_saved_claims(tmp_path, monkeypatch):
    client, output, workspace, launches = setup(tmp_path, monkeypatch)
    before = (output / 'candidates.jsonl').read_bytes()
    response = client.post('/api/pdfs/p/ground-claims', json={'thinking': {'pathway_assembly': False}})
    assert response.status_code == 202
    job = response.json()
    assert job['action'] == 'ground_claims'
    assert not job['options']['thinking']['pathway_assembly']
    assert 'candidate_ids' not in job['options']
    assert len(launches) == 1 and len(workspace._jobs) == 1
    assert (output / 'candidates.jsonl').read_bytes() == before
    assert (output / 'pipeline_checkpoint.json').exists()
    assert not (output / 'ontology_grounding.jsonl').exists()  # No synchronous/automatic model call.


def test_manual_grounding_rejects_missing_or_partial_extraction(tmp_path, monkeypatch):
    client, output, _, launches = setup(tmp_path, monkeypatch)
    save_json(output / 'pipeline_checkpoint.json', {'extraction_complete': False})
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    saved_extraction(output)
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'failed'}])
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'completed'}])
    save_rows(output / 'candidates.jsonl', [])
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    assert launches == []


def test_manual_grounding_rejects_held_or_invalid_fixed_predicates(tmp_path, monkeypatch):
    client, output, _, launches = setup(tmp_path, monkeypatch)
    save_rows(output / 'candidates.jsonl', [closed_claim('held', extraction_review_status='held')])
    response = client.post('/api/pdfs/p/ground-claims', json={})
    assert response.status_code == 409
    assert 'no unheld saved claims' in response.json()['detail']
    save_rows(output / 'candidates.jsonl', [closed_claim('wrong', proposed_relation_iri=CLOSED_PREDICATES['P8']['iri'])])
    response = client.post('/api/pdfs/p/ground-claims', json={})
    assert response.status_code == 409
    assert 'incompatible fixed predicate' in response.json()['detail']
    assert launches == []


def test_manual_grounding_has_no_claim_selection_option(tmp_path, monkeypatch):
    client, _, _, launches = setup(tmp_path, monkeypatch)
    assert client.post('/api/pdfs/p/ground-claims', json={'candidate_ids': ['c1']}).status_code == 422
    assert launches == []


def test_manual_grounding_cannot_overlap_another_job(tmp_path, monkeypatch):
    client, _, workspace, launches = setup(tmp_path, monkeypatch)
    workspace._jobs['existing'] = {'status': 'running'}
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    assert launches == []


def test_worker_routes_manual_grounding_without_invoking_extraction(tmp_path, monkeypatch):
    from backend.api import app as module
    from backend.api.contracts import GroundingOptions
    calls = []
    monkeypatch.setattr(module, '_jobs', {'j': {'pdf_path': str(tmp_path / 'paper.pdf'), 'status': 'queued', 'action': 'ground_claims'}})
    monkeypatch.setattr(module, '_persist_job', lambda *_: None)
    monkeypatch.setattr(module, '_emit', lambda *_: None)
    monkeypatch.setattr(module, '_output_dir', lambda _: tmp_path)
    monkeypatch.setattr(module, 'assemble_saved_pathways', lambda *args, **kwargs: calls.append(kwargs) or {'run_status': 'completed'})
    def forbidden(*args, **kwargs):
        raise AssertionError('Manual grounding must not run extraction or claim repair')
    monkeypatch.setattr(module, 'run_pipeline', forbidden)
    monkeypatch.setattr(module, 'run_claim_action', forbidden)
    options = GroundingOptions(thinking={'pathway_assembly': False})
    module._worker('j', options)
    assert module._jobs['j']['status'] == 'completed'
    assert calls == [{'thinking': options.thinking.model_dump()}]


def test_saved_grounding_accepts_legacy_v9_and_new_v12_checkpoints(tmp_path):
    from backend.pipelines.pathways import saved_grounding_claims

    legacy = tmp_path / 'legacy_v25'
    legacy.mkdir()
    save_json(legacy / 'pipeline_checkpoint.json', {
        'extraction_complete': True,
        'pdf_sha256': 'offline-pdf',
        'extraction_contract_version': 25,
        'predicate_set_version': 'closed-ro-bfo-9-v1',
        'predicate_whitelist_sha256': 'a638c3dd0f8ea56296ca190953c16d2356e629f2265631ccb70ac505a8650370',
    })
    save_rows(legacy / 'extraction_packets.jsonl', [{'status': 'completed'}])
    save_rows(legacy / 'candidates.jsonl', [
        closed_claim('c-legacy', extraction_contract_version=25, predicate_set_version='closed-ro-bfo-9-v1'),
    ])
    assert [c['candidate_id'] for c in saved_grounding_claims(legacy)] == ['c-legacy']

    current = tmp_path / 'current_v26'
    current.mkdir()
    saved_extraction(current)
    p10 = CLOSED_PREDICATES['P10']
    save_rows(current / 'candidates.jsonl', [
        closed_claim('c-p10', subject='phosphate exposure', relation=p10['display_label'], object='nephrocalcinosis',
                     predicate_alias='P10', proposed_relation_iri=p10['iri'], proposed_relation_id=p10['id']),
    ])
    assert [c['candidate_id'] for c in saved_grounding_claims(current)] == ['c-p10']


def test_saved_grounding_uses_all_unheld_claims_without_rewriting_extraction(tmp_path, monkeypatch):
    from backend.pipelines import pathways
    output = tmp_path / 'paper'; output.mkdir()
    observation = {'candidate_id': 'comparison', 'subject': 'Group A', 'relation': 'had less',
                   'object': 'Trait Q', 'extraction_review_status': 'not_flagged', 'source_pdf_sha256': 'offline-pdf',
                   'extraction_contract_version': MEASURED_OBSERVATION_CONTRACT_VERSION,
                   'measured_comparison': {'comparison_axis': 'group', 'direction': 'lower',
                                           'statistical_significance': 'not_reported', 'comparator_quote': ''}}
    candidates = [closed_claim(), closed_claim('held', extraction_review_status='held'), closed_claim('c2'), observation]
    for claim in candidates:
        claim['extraction_contract_version'] = MEASURED_OBSERVATION_CONTRACT_VERSION
    saved_extraction(output, MEASURED_OBSERVATION_CONTRACT_VERSION)
    save_rows(output / 'candidates.jsonl', candidates)
    save_json(output / 'summary.json', {'extracted': len(candidates), 'extraction_coverage': {'complete': True}})
    original = (output / 'candidates.jsonl').read_bytes()
    clients, seen = [], []
    class Client:
        model = 'offline-ground-model'
        request_metrics = {'requests': 1}
        def __init__(self, settings, **kwargs): clients.append(kwargs['task'])
    def ground(root, path, rows, client, settings, emit, *, preserve_unselected):
        seen.extend(rows)
        assert settings.task_thinking['pathway_assembly'] is False
        assert preserve_unselected
        return {'counts': {'grounded_mentions': 2}}, {'mapped_claims': 1}
    monkeypatch.setattr(pathways, 'OpenCodeClient', Client)
    monkeypatch.setattr(pathways, 'run_grounding', ground)
    result = pathways.assemble_saved_pathways(tmp_path, output, lambda *args: None,
                                              thinking={'pathway_assembly': False})
    summary = json.loads((output / 'summary.json').read_text())
    assert clients == ['pathway_assembly'] and seen == [candidates[0], candidates[2], observation]
    assert (output / 'candidates.jsonl').read_bytes() == original
    assert summary['extraction_coverage']['complete'] and summary['extracted'] == 4
    assert summary['grounding_execution'] == 'manual'
    assert summary['ontology_grounding']['assessed'] and summary['ontology_grounding']['status'] == 'completed'
    assert result['grounding_execution'] == 'manual'
    assert summary['pathway_assembly']['status'] == 'paused'
    assert summary['graph']['edges'] == 0
