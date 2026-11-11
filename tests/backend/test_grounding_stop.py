"""Pausing downstream work keeps mappings reviewable and historical files intact."""
from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.pipelines.ontology import rebuild, DIRECTORY
from backend.pipelines.unvalidated import publish_unvalidated_views
from backend.shared.artifacts import save_rows
from backend.shared.ontology_browser import create_router
from backend.stages.ontology_grounding.grounder import ground_claims
from tests.backend.test_ontology_grounding import claim, Grounder


def forbidden(*args, **kwargs):
    raise AssertionError('Paused downstream stage was executed')


def test_rebuild_indexes_mappings_but_never_assembles_or_reasons(tmp_path, monkeypatch):
    reference = Catalog(RESOURCE_DIR)
    output = tmp_path / 'paper'
    output.mkdir()
    sources = [claim('first'), claim('second')]
    save_rows(output / 'candidates.jsonl', sources)
    ground_claims(sources, output, reference, Grounder())
    folder = tmp_path / DIRECTORY
    folder.mkdir()
    historical = {'reasoning.json': b'{"historical": true}', 'assertions.ttl': b'# historical'}
    for name, content in historical.items():
        (folder / name).write_bytes(content)
    monkeypatch.setattr('backend.pipelines.ontology.reason', forbidden)
    monkeypatch.setattr('backend.pipelines.ontology.export_rdf', forbidden)
    state, reasoning = rebuild(tmp_path, reference)
    assert len(state['assertions']) == 2 and state['entities']
    assert state['counts']['mapped_claims'] == 2
    assert state['graph']['nodes'] == state['graph']['edges'] == []
    assert reasoning['status'] == 'paused' and reasoning['coverage']['assessed'] is False
    assert all(state['stage_status'][stage] == 'paused' for stage in
               ('pathway_assembly', 'graph_construction', 'reasoning'))
    assert (folder / 'snapshot.json').exists()
    for name, content in historical.items():
        assert (folder / name).read_bytes() == content


def test_graph_publisher_does_not_read_snapshot_or_write_files(tmp_path, monkeypatch):
    path = tmp_path / 'graph.json'
    path.write_bytes(b'{"historical":true}')
    monkeypatch.setattr('backend.pipelines.unvalidated.write_graphml', forbidden)
    result = publish_unvalidated_views(tmp_path, {})
    assert result['status'] == 'paused' and result['counts']['edges'] == 0
    assert path.read_bytes() == b'{"historical":true}'
    assert not (tmp_path / 'knowledge_graph.graphml').exists()


def test_browser_hides_old_inferences_and_exports_but_keeps_terms(tmp_path, monkeypatch):
    from backend.shared import ontology_browser as browser
    reference = Catalog(RESOURCE_DIR)
    monkeypatch.setattr(browser, 'catalog', lambda: reference)
    folder = tmp_path / DIRECTORY
    folder.mkdir()
    (folder / 'reasoning.json').write_text('{"facts":[{"id":"old-proof"}]}', encoding='utf-8')
    (folder / 'assertions.ttl').write_text('# historical', encoding='utf-8')
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={})
    app = FastAPI()
    app.include_router(create_router(workspace))
    with TestClient(app) as client:
        status = client.get('/api/ontology/status').json()
        assert status['stage_status']['reasoning'] == 'paused'
        assert status['reasoning'] == {}
        assert client.get('/api/ontology/terms?limit=2').json()['terms']
        result = client.get('/api/ontology/reasoning').json()
        assert result['status'] == 'paused' and result['facts'] == []
        assert result['coverage']['assessed'] is False
        assert client.get('/api/ontology/proof?id=old-proof').status_code == 409
        assert client.get('/api/ontology/export').status_code == 409
    assert 'old-proof' in (folder / 'reasoning.json').read_text(encoding='utf-8')
