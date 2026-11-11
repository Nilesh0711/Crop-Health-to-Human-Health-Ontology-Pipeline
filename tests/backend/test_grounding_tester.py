import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.grounding_tester import create_router
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl
from tests.backend.test_ontology_grounding import Grounder, claim


@pytest.fixture
def tester(tmp_path):
    root = tmp_path / 'ontology'
    document = root / 'paper'
    document.mkdir(parents=True)
    rows = [claim('a', source_evidence_span='Dry matter (DM) increased.'),
            claim('b', subject='DM', source_evidence_span='DM increased.'),
            claim('invalid', schema_error='not usable')]
    save_rows(document / 'candidates.jsonl', rows)
    save_rows(document / 'ontology_grounding.jsonl', [])
    def pdf(identifier):
        from fastapi import HTTPException
        if identifier != 'paper-id':
            raise HTTPException(404, 'Unknown PDF')
        return Path('paper.pdf')
    workspace = SimpleNamespace(ONTOLOGY_DIR=root, _lock=RLock(), _jobs={},
        _pdf_path=pdf, _output_dir=lambda path: document)
    clients = []
    def factory(settings, **kwargs):
        fake = Grounder()
        fake.settings = settings
        clients.append(fake)
        return fake
    app = FastAPI()
    app.include_router(create_router(workspace, client_factory=factory, launch=lambda fn: fn()))
    with TestClient(app) as client:
        yield client, workspace, clients


def test_tester_isolates_outputs_and_repeats_frozen_claims(tester):
    client, workspace, fakes = tester
    original = (workspace.ONTOLOGY_DIR / 'paper/candidates.jsonl').read_bytes()
    choices = client.get('/api/ontology/tester/claims?pdf_id=paper-id').json()
    assert choices['total'] == 2
    started = client.post('/api/ontology/tester/tests', json={'pdf_id': 'paper-id', 'claim_ids': ['b'], 'thinking': False})
    assert started.status_code == 202
    test_id = started.json()['id']
    detail = client.get('/api/ontology/tester/tests/' + test_id).json()
    assert detail['status'] == 'completed' and len(detail['results']) == 1
    assert detail['results'][0]['abbreviation_definitions'][0]['expansion'] == 'Dry matter'
    assert fakes[0].settings.task_thinking['pathway_assembly'] is False
    save_rows(workspace.ONTOLOGY_DIR / 'paper/candidates.jsonl', [claim('changed')])
    repeated = client.post(f'/api/ontology/tester/tests/{test_id}/repeat', json={'thinking': True}).json()
    second = client.get('/api/ontology/tester/tests/' + repeated['id']).json()
    assert second['sample'] == detail['sample'] and second['results'][0]['candidate_id'] == 'b'
    assert fakes[1].settings.task_thinking['pathway_assembly'] is True
    assert (workspace.ONTOLOGY_DIR / 'paper/ontology_grounding.jsonl').read_text().strip() == ''
    assert not (workspace.ONTOLOGY_DIR / '.ontology_grounding').exists()
    assert original != (workspace.ONTOLOGY_DIR / 'paper/candidates.jsonl').read_bytes()  # Only this test changed it.
    review = client.post(f'/api/ontology/tester/tests/{test_id}/review/b', json={'decision': 'needs_review', 'note': 'Check aggregation'})
    assert review.status_code == 200
    assert client.get(f'/api/ontology/tester/tests/{test_id}').json()['reviews']['b']['decision'] == 'needs_review'
    assert second['reviews'] == {}  # Reviewer labels never transfer to a repeat.


def test_invalid_selection_and_active_run_are_rejected(tester):
    client, workspace, _ = tester
    for ids, code in [(['missing'], 409), (['a', 'a'], 422), ([], 422), (['a'] * 13, 422)]:
        assert client.post('/api/ontology/tester/tests', json={'pdf_id': 'paper-id', 'claim_ids': ids}).status_code == code
    workspace._jobs['active'] = {'status': 'running'}
    assert client.post('/api/ontology/tester/tests', json={'pdf_id': 'paper-id', 'claim_ids': ['a']}).status_code == 409
    assert client.get('/api/ontology/tester/tests/not-a-valid-id').status_code == 404
    assert not list((workspace.ONTOLOGY_DIR.parent / 'analysis/grounding_tester').glob('*/test.json'))


def test_saved_run_is_unchanged_and_test_has_reproducible_source(tester):
    client, workspace, _ = tester
    original = {p.name: p.read_bytes() for p in (workspace.ONTOLOGY_DIR / 'paper').iterdir()}
    result = client.post('/api/ontology/tester/tests', json={'pdf_id': 'paper-id', 'claim_ids': ['a', 'b']}).json()
    path = workspace.ONTOLOGY_DIR.parent / 'analysis/grounding_tester' / result['id']
    assert {p.name: p.read_bytes() for p in (workspace.ONTOLOGY_DIR / 'paper').iterdir()} == original
    sample = read_jsonl(path / 'sample.jsonl')
    assert len(sample) == 2 and all(r['catalog_signature'] and r['claim_signature'] for r in sample)
    assert json.loads((path / 'test.json').read_text())['status'] == 'completed'


def test_mapping_group_filters_counts_and_unassessed_or_stale_are_distinct(tester):
    from backend.ontology.catalog import catalog
    from backend.ontology.signatures import claim_signature
    client, workspace, _ = tester
    directory = workspace.ONTOLOGY_DIR / 'paper'
    claims = [claim(key, subject=key) for key in ('none', 'some', 'all', 'unassessed', 'old', 'changed')]
    save_rows(directory / 'candidates.jsonl', claims)
    mappings = []
    for source, number in zip(claims[:3], (0, 2, 3)):
        mappings.append({'candidate_id': source['candidate_id'], 'catalog_signature': catalog().signature,
            'claim_signature': claim_signature(source), 'bindings': {role: {
                'mapping': 'exact' if index < number else 'broader', 'iri': 'urn:example:class'}
                for index, role in enumerate(('subject', 'relation', 'object'))}})
    for source in claims[4:]:
        mappings.append({'candidate_id': source['candidate_id'], 'catalog_signature':
            'old-reference' if source['candidate_id'] == 'old' else catalog().signature,
            'claim_signature': 'old-claim', 'bindings': {}})
    save_rows(directory / 'ontology_grounding.jsonl', mappings)
    expected = {'all': 6, 'no_exact': 1, 'some_exact': 1, 'all_exact': 1, 'not_grounded': 1, 'stale': 2}
    for group, identifiers in [('no_exact', ['none']), ('some_exact', ['some']), ('all_exact', ['all']),
                               ('not_grounded', ['unassessed']), ('stale', ['old', 'changed'])]:
        data = client.get('/api/ontology/tester/claims', params={'pdf_id': 'paper-id', 'coverage': group}).json()
        assert [r['candidate_id'] for r in data['claims']] == identifiers
        assert data['coverage_counts'] == expected
    data = client.get('/api/ontology/tester/claims', params={'pdf_id': 'paper-id', 'limit': 1}).json()
    assert len(data['claims']) == 1 and data['total'] == 6
    assert client.get('/api/ontology/tester/claims', params={'pdf_id': 'paper-id', 'coverage': 'invalid'}).status_code == 422
