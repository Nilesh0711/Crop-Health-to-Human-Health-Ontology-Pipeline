"""Offline representation/coverage tests; no claim of model scientific accuracy."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from threading import RLock
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph
from rdflib.namespace import RDF

from backend.domain.classification import classification_complete, classification_counts, CLASSIFICATION_POLICY
from backend.domain.extraction_review import assertion_eligible
from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.ontology.registry import relation_registry, entity_registry
from backend.pipelines.ontology import rebuild, export_rdf, current_assertions
from backend.shared.artifacts import save_rows
from backend.shared.ontology_browser import create_router
from backend.stages.ontology_grounding import rows as wire
from backend.stages.ontology_grounding.contract import packet_schema, check_response, GROUNDING_INSTRUCTIONS
from backend.stages.ontology_grounding.grounder import ground_claims, request_packet
from backend.stages.ontology_reasoning.engine import reason
from tests.backend.test_ontology_grounding import Grounder, claim

BASE = 'http://purl.obolibrary.org/obo/'


@pytest.fixture(scope='module')
def reference():
    return Catalog(RESOURCE_DIR)


def test_shared_classification_policy_conformance():
    data = json.loads((Path(__file__).parents[1] / 'fixtures/classification_policy_v1.json').read_text())
    ref = SimpleNamespace(profile=data)
    for case in data['cases']:
        before = deepcopy(case['row'])
        assert classification_complete(case['row'], ref) is case['classification_complete'], case['case_id']
        assert assertion_eligible(case['row']) is case['assembly_eligible'], case['case_id']
        assert case['row'] == before
    assert classification_counts([c['row'] for c in data['cases']], ref) == {
        'measured_comparisons': 0, 'complete_measured_comparisons': 0,
        'classified_endpoint_mentions': 20,
        'endpoint_review_warning_mentions': 0,
        'mapping_review_warning_claims': 0,
        'classified_relation_mentions': 7,
        'exact_relation_mentions': 6, 'broader_relation_mentions': 1,
        'proposed_relation_under_review_mentions': 2,
        'property_type_conflict_claims': 0, 'property_type_missing_endpoint_claims': 0,
        'property_type_review_claims': 0,
        'classified_claims': 4, 'classification_review_claims': 7}


@pytest.mark.parametrize('mapping', ['exact', 'broader', 'unresolved'])
def test_both_protocols_accept_the_same_relation_categories(reference, mapping):
    payload, _ = request_packet([claim(subject='root', object='root system', relation='is directly part of')], reference)
    answer = Grounder().chat_json('', json.dumps(payload), response_check=lambda _: None)
    part = next(k for k, t in payload['relations'].items() if t['label'] == 'part of')
    result = answer['results'][0]
    result['relation'].update(mapping=mapping, term=None if mapping == 'unresolved' else part)
    assert check_response(answer, payload)
    lines = ['C1.scope\tscope\tin_scope\t-\tFixture scope.']
    for role in ['subject', 'object', 'relation']:
        b = result[role]
        kind = 'relation' if role == 'relation' else result[role + '_denotation']
        lines.append('\t'.join(['C1.' + role, kind, b['mapping'], b['term'] or '-', b['reason']]))
    assert check_response(wire.parse_rows('\n'.join(lines), payload), payload)
    assert packet_schema(payload)['properties']['results']['items']['properties']['relation']['properties']['mapping']['enum'] == ['exact', 'broader', 'unresolved']


@pytest.mark.parametrize('mapping,term', [('related', 'R1'), ('narrower', 'R1'), ('broader', 'R999'), ('broader', None)])
def test_relation_categories_and_targets_are_constrained(reference, mapping, term):
    payload, _ = request_packet([claim()], reference)
    answer = Grounder().chat_json('', json.dumps(payload), response_check=lambda _: None)
    answer['results'][0]['relation'].update(mapping=mapping, term=term)
    with pytest.raises(ValueError):
        check_response(answer, payload)


class BroaderGrounder(Grounder):
    def chat_json(self, system, user, **kwargs):
        answer = super().chat_json(system, user, response_check=lambda _: None)
        payload = json.loads(user)
        part = next(k for k, t in payload['relations'].items() if t['label'] == 'part of')
        for row in answer['results']:
            row['relation'].update(mapping='broader', term=part, reason='Generic part-of omits the stated directness.')
        kwargs['response_check'](answer)
        return answer


def source_claim(identifier='one'):
    return claim(identifier, subject='root', object='root system', relation='is directly part of',
                 qualifiers={'population': 'Group A'}, source_evidence_span='The root is directly part of the root system.')


def test_broader_relations_preserve_source_and_do_not_export_a_predicate(tmp_path, reference):
    c = source_claim()
    before = deepcopy(c)
    records = ground_claims([c], tmp_path, reference, BroaderGrounder())
    row = records[0]
    assert c == before and row['original_claim'] == before
    assert row['classification_policy'] == CLASSIFICATION_POLICY
    assert classification_complete(row, reference) and not row['assembly_eligible']
    assert len(entity_registry(records)) == 2
    target = tmp_path / 'assertions.ttl'
    export_rdf(target, reference, records)
    assert not list(Graph().parse(target).triples((None, RDF.predicate, None)))
    registry = relation_registry(records, reference)
    assert registry['summary']['broader_mentions'] == 1
    assert registry['summary']['grounded_mentions'] == 0
    assert registry['extracted'][0]['status'] == 'classification'


def test_exact_and_broader_groups_keep_separate_categories(tmp_path, reference):
    records = ground_claims([source_claim('a'), source_claim('b')], tmp_path, reference, BroaderGrounder())
    records[1]['bindings']['relation']['mapping'] = 'exact'
    reg = relation_registry(records, reference)
    assert {r['mapping'] for r in reg['extracted']} == {'exact', 'broader'}
    assert reg['summary']['classified_mentions'] == 2 and reg['summary']['classified_properties'] == 1


def test_vacuole_import_is_upstream_nonobsolete_and_bounded(reference):
    iri = BASE + 'GO_0005773'
    term = reference.terms[iri]
    assert len(reference.profile['allowed_classes']) == 212
    assert len(reference.profile['allowed_object_properties']) == 102
    assert term['selectable'] and not term['deprecated'] and term['definition']
    assert term['label'] == 'vacuole' and term['sources']
    assert BASE + 'TO_0000627' not in reference.profile['allowed_classes']
    assert any(m.get('requested_iri') == iri and m['source'].startswith('https://ontobee.org/') and len(m['sha256']) == 64
               for m in reference.manifest['modules'])


def test_live_classification_filters_and_manual_review_keep_exact_gate(tmp_path, reference, monkeypatch):
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [source_claim()])
    ground_claims([source_claim()], output, reference, BroaderGrounder())
    rebuild(tmp_path, reference)
    monkeypatch.setattr('backend.shared.ontology_browser.catalog', lambda: reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={}, _output_dir=lambda _: output,
                                _pdf_path=lambda _: tmp_path / 'paper.pdf', rebuild_ontology=rebuild)
    app = FastAPI(); app.include_router(create_router(workspace))
    with TestClient(app) as client:
        relations = client.get('/api/ontology/relations?status=classified').json()
        assert relations['total'] == 1 and relations['status_counts']['classification'] == 1
        assert client.get('/api/ontology/relations?status=grounded').json()['total'] == 0
        row = client.get('/api/ontology/assertions').json()['assertions'][0]
        assert row['classification_complete'] and not row['assembly_eligible']
        data = {'role': 'relation', 'mapping': 'broader', 'iri': BASE + 'BFO_0000050',
                'note': 'Reviewed weaker alignment.', 'expected_revision': row['revision']}
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=data).status_code == 200
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=data).status_code == 409
        row = client.get('/api/ontology/assertions').json()['assertions'][0]
        assert row['classification_complete'] and not row['assembly_eligible']
        data.update(expected_revision=row['revision'], mapping='related')
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=data).status_code == 422
    viewer = FastAPI(); viewer.include_router(create_router(workspace, read_only=True))
    with TestClient(viewer) as client:
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=data).status_code == 403


def test_prompt_preserves_property_requirements_and_no_forced_coverage():
    assert 'execution/necessity requirements' in GROUNDING_INSTRUCTIONS
    assert 'source assertion entails' in GROUNDING_INSTRUCTIONS
    assert 'never optimize the number' in GROUNDING_INSTRUCTIONS
    assert 'asserted causation or correlation' in GROUNDING_INSTRUCTIONS


def test_legacy_eligible_broader_relation_cannot_enter_reasoning(tmp_path, reference):
    records = ground_claims([source_claim()], tmp_path, reference, BroaderGrounder())
    records[0]['assembly_eligible'] = True  # Deliberately corrupt historical admission flag.
    minimal = SimpleNamespace(graph=Graph(), profile=reference.profile, manifest={},
                              signature=reference.signature, rules_signature=reference.rules_signature)
    result = reason(minimal, records)
    assert not result['facts']
    assert result['coverage']['skipped_assertions'][0]['candidate_id'] == 'one'


def test_common_parent_never_shares_entity_identity(tmp_path, reference):
    records = ground_claims([source_claim('a'), source_claim('b')], tmp_path, reference, BroaderGrounder())
    for row in records:
        row['bindings']['subject']['mapping'] = 'broader'
    entities = entity_registry(records)
    subjects = [e for e in entities if e['mapping'] == 'broader']
    assert len(subjects) == 2 and not any(e['shared_concept'] for e in subjects)


def test_classification_policy_change_is_stale_and_configured_reassessment(tmp_path, reference, monkeypatch):
    output = tmp_path / 'paper'; output.mkdir()
    claims = [source_claim()]
    save_rows(output / 'candidates.jsonl', claims)
    rows = ground_claims(claims, output, reference, BroaderGrounder())
    assert len(current_assertions(tmp_path, reference)[0]) == 1
    rows[0].pop('classification_policy')
    save_rows(output / 'ontology_grounding.jsonl', rows)
    assert not current_assertions(tmp_path, reference)[0]
    monkeypatch.setattr('backend.stages.ontology_grounding.grounder.CLASSIFICATION_POLICY', 'test-changed-policy')
    replaced = ground_claims(claims, output, reference, BroaderGrounder())
    assert replaced[0]['config_signature'] != rows[0]['config_signature']
    assert replaced[0]['classification_policy'] == 'test-changed-policy'


def test_development_controls_are_separate_and_paused_defaults_remain():
    from backend.cli.appendix_examples import examples
    from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED
    from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION
    controls = json.loads((Path(__file__).parents[1] / 'fixtures/classification_v15_controls.json').read_text())
    assert len(controls['cases']) == 10 and controls['review_status'] == 'pending'
    assert POST_GROUNDING_STAGES_PAUSED and EXTRACTION_CONTRACT_VERSION == 20
    assert wire.VERSION == 'grounding-tsv-v2'
    assert 'profile v16' in examples()['groundingExample']['versionNote']
    assert CLASSIFICATION_POLICY in examples()['groundingExample']['versionNote']
