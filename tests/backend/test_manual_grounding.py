"""Manual grounding queues saved extraction, never starts a new extraction."""
import json
from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.runs import create_router
from backend.shared.artifacts import save_rows, save_json


def setup(tmp_path, monkeypatch):
    from backend.api.routes import runs
    output = tmp_path / 'paper'; output.mkdir()
    save_json(output / 'pipeline_checkpoint.json', {'extraction_complete': True})
    save_rows(output / 'candidates.jsonl', [{'candidate_id': 'c1', 'subject': 'S', 'relation': 'affects', 'object': 'O'}])
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'completed'}])
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
    assert len(launches) == 1 and len(workspace._jobs) == 1
    assert (output / 'candidates.jsonl').read_bytes() == before
    assert (output / 'pipeline_checkpoint.json').exists()
    assert not (output / 'ontology_grounding.jsonl').exists()  # No synchronous/automatic model call.


def test_manual_grounding_rejects_missing_or_partial_extraction(tmp_path, monkeypatch):
    client, output, _, launches = setup(tmp_path, monkeypatch)
    save_json(output / 'pipeline_checkpoint.json', {'extraction_complete': False})
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    save_json(output / 'pipeline_checkpoint.json', {'extraction_complete': True})
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'failed'}])
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    save_rows(output / 'extraction_packets.jsonl', [{'status': 'completed'}])
    save_rows(output / 'candidates.jsonl', [])
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    assert launches == []


def test_manual_grounding_cannot_overlap_another_job(tmp_path, monkeypatch):
    client, _, workspace, launches = setup(tmp_path, monkeypatch)
    workspace._jobs['existing'] = {'status': 'running'}
    assert client.post('/api/pdfs/p/ground-claims', json={}).status_code == 409
    assert launches == []


def test_worker_routes_manual_grounding_without_invoking_extraction(tmp_path, monkeypatch):
    from backend.api import app as module
    from backend.api.contracts import RunOptions
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
    options = RunOptions(thinking={'pathway_assembly': False})
    module._worker('j', options)
    assert module._jobs['j']['status'] == 'completed'
    assert calls == [{'thinking': options.thinking.model_dump()}]


def test_saved_grounding_updates_mappings_without_rewriting_extraction(tmp_path, monkeypatch):
    from backend.pipelines import pathways
    output = tmp_path / 'paper'; output.mkdir()
    candidates = [{'candidate_id': 'c1', 'subject': 'S', 'relation': 'affects', 'object': 'O'}]
    save_rows(output / 'candidates.jsonl', candidates)
    save_json(output / 'summary.json', {'extracted': 1, 'extraction_coverage': {'complete': True}})
    original = (output / 'candidates.jsonl').read_bytes()
    clients, seen = [], []
    class Client:
        model = 'offline-ground-model'
        request_metrics = {'requests': 1}
        def __init__(self, settings, **kwargs): clients.append(kwargs['task'])
    def ground(root, path, rows, client, settings, emit):
        seen.extend(rows)
        assert settings.task_thinking['pathway_assembly'] is False
        return {'counts': {'grounded_mentions': 2}}, {'mapped_claims': 1}
    monkeypatch.setattr(pathways, 'OpenCodeClient', Client)
    monkeypatch.setattr(pathways, 'run_grounding', ground)
    result = pathways.assemble_saved_pathways(tmp_path, output, lambda *args: None,
                                              thinking={'pathway_assembly': False})
    summary = json.loads((output / 'summary.json').read_text())
    assert clients == ['pathway_assembly'] and seen == candidates
    assert (output / 'candidates.jsonl').read_bytes() == original
    assert summary['extraction_coverage']['complete'] and summary['extracted'] == 1
    assert summary['grounding_execution'] == 'manual'
    assert summary['ontology_grounding']['assessed'] and summary['ontology_grounding']['status'] == 'completed'
    assert result['grounding_execution'] == 'manual'
    assert summary['pathway_assembly']['status'] == 'paused'
    assert summary['graph']['edges'] == 0
