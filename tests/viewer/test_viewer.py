"""Exercise publication integrity and read-only behavior without paid model calls."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from backend.viewer.app import create_app
from backend.viewer.export import export_snapshot, source_inventory, clean
from backend.viewer.snapshot import Snapshot, manifest_id
from backend.shared.storage import stable_hash


def save(path, value, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(('\n'.join(json.dumps(row) for row in value) if jsonl else json.dumps(value)), encoding='utf-8')


@pytest.fixture
def source(tmp_path):
    ontology, pdfs, output = tmp_path / 'ontology', tmp_path / 'pdf', tmp_path / 'publication'
    folder = ontology / 'Study C'
    folder.mkdir(parents=True); pdfs.mkdir()
    (pdfs / 'Study C.pdf').write_bytes(b'%PDF-1.4\nfixture')
    identifier = stable_hash('Study C.pdf', 16)
    candidate = {'candidate_id': 'claim-c', 'subject': 'Crop C', 'relation': 'contains', 'object': 'Compound T',
                 'subject_type': 'Crop', 'object_type': 'Compound', 'source_validation': {'decision': 'entailed'}}
    save(folder / 'candidates.jsonl', [candidate], True)
    save(folder / 'summary.json', {'run_status': 'completed', 'extracted': 1})
    save(folder / 'run_state.json', {'id': 'job-current', 'status': 'completed'})
    for job_id, status in [('job-current', 'completed'), ('job-old', 'running')]:
        save(folder / 'jobs' / (job_id + '.json'), {'id': job_id, 'pdf_id': identifier, 'status': status,
             'logs': [{'level': 'error', 'message': 'Example saved warning', 'at': '2026-01-01'}],
             'options': {'thinking': {'external': False}}, 'pdf_path': 'private-machine-path',
             'request_progress': {'status': 'waiting_for_provider'}})
    return ontology, pdfs, output, identifier


def publish(source, **kwargs):
    ontology, pdfs, output, _ = source
    return export_snapshot(ontology, pdfs, output, **kwargs)


def test_export_is_read_only_and_preserves_views_and_archives(source):
    ontology, pdfs, output, identifier = source
    before = source_inventory(ontology, pdfs)
    result = publish(source, include_pdfs=True)
    assert source_inventory(ontology, pdfs) == before
    assert result['documents'] == 1
    snapshot = Snapshot(output).load()
    entry = snapshot.document(identifier)
    candidate = snapshot.read(entry['claims'])['claims'][0]
    assert candidate['candidate']['subject'] == 'Crop C'
    assert candidate['repair_eligibility']['allowed'] is False
    old = snapshot.read(entry['jobs']['job-old'])
    assert old['status'] == 'snapshot' and old['recorded_status'] == 'running'
    assert 'request_progress' not in old and 'pdf_path' not in old
    assert old['options']['thinking']['external'] is False
    assert snapshot.bytes(entry['pdf']).startswith(b'%PDF')


def test_viewer_routes_and_no_execution(source, tmp_path):
    publish(source, include_pdfs=True)
    _, _, output, identifier = source
    dist = tmp_path / 'dist'; dist.mkdir()
    (dist / 'index.html').write_text('<html>Results frontend</html>')
    with TestClient(create_app(output, dist)) as client:
        health = client.get('/api/health').json()
        assert health['capabilities'] == {'read_only': True, 'execute': False, 'upload': False}
        assert client.get('/').status_code == 200
        for kind in ('claims', 'logs', 'graph', 'audit-graph', 'source-graph', 'paths', 'trust', 'jobs', 'file'):
            assert client.get(f'/api/pdfs/{identifier}/{kind}').status_code == 200, kind
        for path in ('/api/graph', '/api/source-graph', '/api/audit-graph', '/api/pathways?view=provisional&max_hops=12&offset=3', '/api/jobs/job-old'):
            assert client.get(path).status_code == 200, path
        assert client.get('/api/jobs/unknown').status_code == 404
        assert client.get('/api/pdfs/unknown/claims').status_code == 404
        assert client.get('/api/not-real').status_code == 404
        assert client.get('/api/pathways?max_hops=30').status_code == 422
        assert client.get('/api/pdfs/' + identifier + '/file').content.startswith(b'%PDF')
        for method in ('post', 'put', 'patch', 'delete'):
            for path in ('/api/pdfs', f'/api/pdfs/{identifier}/run', f'/api/pdfs/{identifier}/resume',
                         f'/api/pdfs/{identifier}/claims/claim-c/repair', f'/api/pdfs/{identifier}/assemble-pathways', '/api/jobs/job-old/stop'):
                assert getattr(client, method)(path).status_code == 403
        assert client.get('/api/pdfs/' + identifier + '/recovery').json()['can_continue'] is False
        # Frozen job reads neither rewrite original status nor add interruption logs.
        assert client.get('/api/jobs/job-old').json() == client.get('/api/jobs/job-old').json()


def test_missing_pdf_and_invalid_destination_are_explicit(source):
    _, pdfs, output, identifier = source
    publish(source)
    with TestClient(create_app(output)) as client:
        assert client.get('/api/pdfs').json()[0]['pdf_available'] is False
        assert client.get(f'/api/pdfs/{identifier}/file').status_code == 404
    with pytest.raises(ValueError, match='separate'):
        export_snapshot(source[0], pdfs, pdfs / 'nested')
    with pytest.raises(ValueError, match='already exists'):
        publish(source)


def test_tampered_or_missing_files_fail_closed(source):
    publish(source)
    output = source[2]
    snapshot = Snapshot(output).load()
    path = output / snapshot.manifest['catalog']
    original = path.read_bytes()
    path.write_bytes(original + b' ')
    with pytest.raises(ValueError, match='integrity'):
        Snapshot(output).load()
    path.write_bytes(original)
    with TestClient(create_app(output)) as client:
        path.unlink()
        assert client.get('/api/pdfs').status_code == 503


def test_wrong_job_mapping_rejected_even_with_valid_hashes(source):
    publish(source)
    path = source[2] / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['jobs'] = {}
    manifest['snapshot_id'] = manifest_id(manifest)
    save(path, manifest)
    with pytest.raises(ValueError, match='job index'):
        Snapshot(source[2]).load()


def test_replacement_keeps_previous_and_active_source_does_not_replace(source):
    first = publish(source)
    second = publish(source, replace=True)
    assert Snapshot(second['previous_snapshot']).load().manifest['snapshot_id'] == first['snapshot_id']
    save(source[0] / 'Study C/run_state.json', {'status': 'running'})
    with pytest.raises(ValueError, match='active'):
        publish(source, replace=True)
    assert Snapshot(source[2]).load().manifest['snapshot_id'] == second['snapshot_id']


def test_concurrent_changes_abort_publication(source, monkeypatch):
    import backend.viewer.export as exporter
    original = exporter.source_inventory
    calls = []
    def changed(*args):
        inventory = original(*args)
        calls.append(1)
        return {**inventory, **({'concurrent-change': 'x'} if len(calls) > 1 else {})}
    monkeypatch.setattr(exporter, 'source_inventory', changed)
    with pytest.raises(ValueError, match='changed during export'):
        publish(source)
    assert not source[2].exists()


def test_viewer_import_has_no_inference_dependencies():
    code = "import sys; import backend.viewer.app; bad=[m for m in sys.modules if m.startswith(('backend.api', 'backend.pipelines', 'backend.providers', 'torch', 'transformers', 'sentence_transformers', 'networkx'))]; assert not bad, bad"
    subprocess.run([sys.executable, '-c', code], check=True)


def test_export_strips_credentials_and_local_path():
    assert clean({'api_key': 'secret', 'pdf_path': 'private', 'logs': ['oc_sk_example_secret']}) == {'logs': ['[redacted credential]']}
