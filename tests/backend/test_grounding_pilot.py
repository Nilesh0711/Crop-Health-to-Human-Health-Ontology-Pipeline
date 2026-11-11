"""Small-scope identity, partial grounding and independently reviewed pilot data."""
import json
from types import SimpleNamespace
from threading import RLock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.pipelines.ontology import rebuild
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl
from backend.shared.ontology_browser import create_router
from backend.stages.ontology_grounding.grounder import ground_claims
from backend.stages.evaluation.grounding import evaluate
from backend.stages.evaluation.grounding_pilot import prepare_pilot, run_pilot
from tests.backend.test_ontology_grounding import Grounder, claim

PHOSPHATE = 'http://purl.obolibrary.org/obo/CHEBI_26020'


def test_active_profile_covers_potato_nutrition_with_compact_guidance():
    reference = Catalog(RESOURCE_DIR)
    assert len(reference.profile['allowed_classes']) == 211
    assert len(reference.profile['allowed_object_properties']) == 19
    assert reference.terms['http://purl.obolibrary.org/obo/CHEBI_17632']['selectable']
    assert reference.terms[PHOSPHATE]['selectable']
    instructions = reference.extraction_instructions()
    assert 'potato root, leaf and tuber structures' in instructions
    # Extraction is ontology-agnostic; the class and relation catalogue is supplied only to grounding.
    for iri in reference.profile['allowed_classes'] + reference.profile['allowed_object_properties']:
        assert iri not in instructions
    assert 'Approved class names' not in instructions
    assert 'exact_synonyms' not in instructions and '"domain":' not in instructions


@pytest.mark.parametrize('denotation,expected_entities', [('concept', 3), ('particular', 4), ('state', 4)])
def test_partial_claims_keep_endpoint_identity_and_separate_observations(tmp_path, denotation, expected_entities):
    reference = Catalog(RESOURCE_DIR)
    output = tmp_path / 'paper'; output.mkdir()
    claims = [claim('a', subject='phosphate', object='Unknown outcome Q', relation='unmapped_predicate'),
              claim('b', subject='phosphate', object='Unknown outcome R', relation='unmapped_predicate',
                    qualifiers={'condition': 'Condition R'})]
    save_rows(output / 'candidates.jsonl', claims)
    ground_claims(claims, output, reference, Grounder(denotation=denotation))
    state, _ = rebuild(tmp_path, reference)
    assert state['counts']['grounded_mentions'] == 2
    assert state['counts']['mapped_claims'] == 0
    assert state['counts']['entities'] == expected_entities
    assert state['counts']['shared_concepts'] == (1 if denotation == 'concept' else 0)
    assert not state['graph']['edges'] and not state['graph']['nodes']
    grounded = [e for e in state['entities'] if e['status'] == 'grounded']
    assert all(e['iri'] == PHOSPHATE for e in grounded)
    assert sum(len(e['occurrences']) for e in grounded) == 2
    assert read_jsonl(output / 'candidates.jsonl') == claims


def test_entity_browser_preserves_each_occurrences_own_evidence(tmp_path, monkeypatch):
    reference = Catalog(RESOURCE_DIR)
    output = tmp_path / 'paper'; output.mkdir()
    claims = [claim('a', subject='phosphate', object='Unknown outcome Q', relation='unmapped_predicate', source_evidence_span='Source Q.'),
              claim('b', subject='phosphate', object='Unknown outcome R', relation='unmapped_predicate', source_evidence_span='Source R.')]
    save_rows(output / 'candidates.jsonl', claims)
    ground_claims(claims, output, reference, Grounder())
    rebuild(tmp_path, reference)
    monkeypatch.setattr('backend.shared.ontology_browser.catalog', lambda: reference)
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={})))
    with TestClient(app) as client:
        data = client.get('/api/ontology/entities?status=grounded').json()
        assert data['total'] == 1 and not data['stale']
        occurrences = data['entities'][0]['occurrences']
        assert {o['candidate_id']: o['evidence'] for o in occurrences} == {'a': 'Source Q.', 'b': 'Source R.'}
        assert not any(o['claim_assembly_eligible'] for o in occurrences)
        expected = {'grounded': 1, 'vocabulary_gap': 2, 'alignment_review': 0, 'classification': 0, 'review': 0, 'all': 3}
        assert data['status_counts'] == expected  # Shared mentions count as one registry entry.
        gaps = client.get('/api/ontology/entities?status=vocabulary_gap&limit=1').json()
        assert gaps['total'] == 2 and len(gaps['entities']) == 1
        assert gaps['status_counts'] == expected  # Neither selected filter nor pagination reduces counts.
        searched = client.get('/api/ontology/entities?q=Unknown%20outcome%20Q&status=all').json()
        assert searched['status_counts'] == {'grounded': 0, 'vocabulary_gap': 1, 'alignment_review': 0, 'classification': 0, 'review': 0, 'all': 1}
        assert client.get('/api/ontology/entities?document=missing').json()['status_counts']['all'] == 0


def test_prepare_pilot_is_bounded_unlabelled_and_never_overwrites_labels(tmp_path):
    reference = Catalog(RESOURCE_DIR)
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim(str(i), subject='phosphate', object='root length',
        source_local_context=f'Synthetic context {i}.') for i in range(30)])
    dest = tmp_path / 'pilot.jsonl'
    assert prepare_pilot(tmp_path, reference, dest, document='paper', limit=20)['model_calls'] == 0
    rows = read_jsonl(dest)
    assert len(rows) == len({r['id'] for r in rows}) == 20
    assert all(r['prediction'] is None and not r['gold']['reviewed'] and r['gold']['ontology_iri'] is None for r in rows)
    assert all(r['catalog_signature'] == reference.signature for r in rows)
    with pytest.raises(ValueError, match='already exists'):
        prepare_pilot(tmp_path, reference, dest)
    with pytest.raises(ValueError, match='No extracted'):
        prepare_pilot(tmp_path, reference, tmp_path / 'missing.jsonl', document='not-a-document')


def test_pilot_grounding_never_sends_review_answers_or_modifies_source(tmp_path):
    reference = Catalog(RESOURCE_DIR)
    source = tmp_path / 'source'; output = source / 'paper'; output.mkdir(parents=True)
    save_rows(output / 'candidates.jsonl', [claim(subject='phosphate', object='root length', relation='increases')])
    before = (output / 'candidates.jsonl').read_bytes()
    dest = tmp_path / 'pilot.jsonl'
    prepare_pilot(source, reference, dest)
    sample = read_jsonl(dest)
    sample[0]['gold']['notes'] = 'DO_NOT_SEND_REVIEW_ANSWERS'
    client = Grounder()
    result = run_pilot(sample, reference, tmp_path / 'off', client)
    assert result['source_claims'] == 1
    assert 'DO_NOT_SEND_REVIEW_ANSWERS' not in json.dumps(client.calls)
    assert (output / 'candidates.jsonl').read_bytes() == before
    assert not (output / 'ontology_grounding.jsonl').exists()
    with pytest.raises(ValueError, match='not empty'):
        run_pilot(sample, reference, tmp_path / 'off', client)
    sample[0]['catalog_signature'] = 'changed'
    with pytest.raises(ValueError, match='reference changed'):
        run_pilot(sample, reference, tmp_path / 'on', client)
    assert not (tmp_path / 'on').exists()


def test_review_labels_require_compatible_predictions_and_detect_wrong_merges(tmp_path):
    reference = Catalog(RESOURCE_DIR)
    source = tmp_path / 'source'; output = source / 'paper'; output.mkdir(parents=True)
    save_rows(output / 'candidates.jsonl', [claim('a', subject='phosphate', object='root length', relation='increases'),
        claim('b', subject='phosphate', object='root length', relation='increases', source_local_context='Other context.')])
    sample_path = tmp_path / 'review.jsonl'
    prepare_pilot(source, reference, sample_path)
    sample = [r for r in read_jsonl(sample_path) if r['role'] == 'subject']
    for i, row in enumerate(sample):
        row['gold'].update(reviewed=True, reviewer='Offline test reviewer', ontology_iri=PHOSPHATE,
                           mapping='exact', denotation='concept', identity_group=f'different-{i}')
    save_rows(sample_path, sample)
    report = evaluate(sample_path, root=tmp_path / 'absent')
    assert report['labels_without_compatible_predictions'] == 2 and report['mapping_accuracy'] is None
    run_pilot(sample, reference, tmp_path / 'results', Grounder())
    before = sample_path.read_bytes()
    report = evaluate(sample_path, root=tmp_path / 'results')
    assert report['alignment_accuracy'] == 1 and report['incorrect_merges'] == 1
    assert sample_path.read_bytes() == before
    sample[0]['claim_signature'] = 'changed-source'
    save_rows(sample_path, sample)
    assert evaluate(sample_path, root=tmp_path / 'results')['labels_without_compatible_predictions'] == 1
