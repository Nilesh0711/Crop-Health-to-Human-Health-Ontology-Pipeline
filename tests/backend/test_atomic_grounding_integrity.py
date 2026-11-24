"""Offline integrity checks; mocked outputs do not measure LLM scientific quality."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.cli.appendix_examples import examples, render_examples
from backend.domain.extraction_review import assertion_eligible, refresh_eligibility, ELIGIBILITY_POLICY
from backend.ontology.registry import entity_registry
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA, EXTRACTION_CONTRACT_VERSION
from backend.stages.claim_extraction.extractor import check_extraction_response, extract_claims
from backend.stages.ontology_grounding.contract import packet_schema, check_response
from backend.stages.ontology_grounding import rows as wire_rows
from backend.stages.ontology_grounding.grounder import ground_claims
from backend.stages.ontology_reasoning.engine import reason
from tests.backend.test_ontology_grounding import Grounder, claim, reference as reference
from tests.backend.test_extraction_targets import packet, row


@pytest.mark.parametrize('relation,iri', [('unresolved', None), ('exact', None), ('unresolved', 'urn:predicate')])
def test_entity_grounding_survives_unresolved_relation_without_eligibility(tmp_path, reference, relation, iri):
    rows = ground_claims([claim('a')], tmp_path, reference, Grounder())
    r = rows[0]
    r['bindings']['relation'].update(mapping=relation, iri=iri)
    r['assembly_eligible'] = True  # legacy data cannot override the new rule
    refresh_eligibility(r)
    assert not r['assembly_eligible']
    assert r['eligibility_policy'] == ELIGIBILITY_POLICY
    assert any(e['status'] == 'grounded' for e in entity_registry(rows))
    assert all(not o['claim_assembly_eligible'] for e in entity_registry(rows) for o in e['occurrences'])


@pytest.mark.parametrize('predicate', [None, '', 'urn:unapproved-predicate'])
def test_reasoning_never_publishes_legacy_invalid_predicates(tmp_path, reference, predicate):
    r = ground_claims([claim('a')], tmp_path, reference, Grounder())[0]
    r['bindings']['relation'].update(mapping='exact', iri=predicate)
    r['assembly_eligible'] = True
    result = reason(reference, [r])
    assert all(f['predicate'] not in {'None', '', 'urn:unapproved-predicate'} for f in result['facts'])
    assert result['coverage']['skipped_assertions'][0]['candidate_id'] == 'a'


def test_review_acceptance_cannot_override_unresolved_relation_or_changed_evidence(tmp_path, reference):
    r = ground_claims([claim('a', subject='root', relation='part of', object='root system')], tmp_path, reference, Grounder())[0]
    r['extraction_warnings'] = [{'code': 'fixture'}]
    r['extraction_review'] = {'status': 'accepted', 'claim_signature': r['claim_signature']}
    assert assertion_eligible(r, reference)
    r['property_type_check'] = {'status': 'insufficient_type_info', 'issues': [{'role': 'subject', 'kind': 'insufficient_type_info'}]}
    assert not assertion_eligible(r)
    r.pop('property_type_check', None)
    r['bindings']['relation'].update(mapping='unresolved', iri=None)
    assert not assertion_eligible(r, reference)
    r['claim_signature'] = 'changed-evidence'
    assert not assertion_eligible(r, reference)


def test_old_policy_rows_are_stale_without_rewriting_saved_data(tmp_path, reference):
    from backend.pipelines.ontology import current_assertions
    from backend.shared.artifacts import save_rows
    claims = [claim('a')]
    output = tmp_path / 'paper'
    output.mkdir()
    save_rows(output / 'candidates.jsonl', claims)
    rows = ground_claims(claims, output, reference, Grounder())
    assert len(current_assertions(tmp_path, reference)[0]) == 1
    rows[0]['eligibility_policy'] = 'older-policy'
    save_rows(output / 'ontology_grounding.jsonl', rows)
    before = (output / 'ontology_grounding.jsonl').read_bytes()
    current, stale = current_assertions(tmp_path, reference)
    assert not current and len(stale) == 1
    assert (output / 'ontology_grounding.jsonl').read_bytes() == before


def test_live_viewer_recomputes_legacy_eligibility_without_mutating_snapshot(tmp_path, reference, monkeypatch):
    import json
    from threading import RLock
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.ontology.registry import relation_registry
    from backend.shared.ontology_browser import create_router
    from backend.shared.artifacts import save_json
    rows = ground_claims([claim('a')], tmp_path / 'paper', reference, Grounder())
    rows[0]['bindings']['relation'].update(iri=None, mapping='unresolved')
    rows[0]['assembly_eligible'] = True
    folder = tmp_path / '.ontology_grounding'
    folder.mkdir()
    save_json(folder / 'snapshot.json', {'assertions': rows, 'entities': entity_registry(rows),
              'counts': {'mapped_claims': 1}})
    monkeypatch.setattr('backend.shared.ontology_browser.catalog', lambda: reference)
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={}), read_only=True))
    with TestClient(app) as client:
        assert client.get('/api/ontology/assertions').json()['assertions'][0]['assembly_eligible'] is False
        assert client.get('/api/ontology/status').json()['counts']['mapped_claims'] == 0
        relations = client.get('/api/ontology/relations?status=unresolved').json()
        assert relations['summary']['total_claims'] == 1
        assert relations['relations'][0]['occurrences'][0]['claim_assembly_eligible'] is False
        assert client.post('/api/ontology/claims/paper/a/review', json={}).status_code in {403, 404, 422}
    assert json.loads((folder / 'snapshot.json').read_text())['assertions'][0]['assembly_eligible'] is True
    assert relation_registry(rows, reference)['summary']['unresolved_mentions'] == 1


def test_appendix_tracks_active_contract_and_allows_both_grounding_formats(reference):
    data = examples()
    assert EXTRACTION_CONTRACT_VERSION == 20
    assert set(EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']['required']) == {
        'subject', 'object', 'relation', 'qualifiers', 'evidence_sentence_ids'}
    assert Path('frontend/src/ontology/appendixExamples.js').read_text(encoding='utf-8') == render_examples()
    check_extraction_response(data['extractionExample']['output'], EXTRACTION_RESPONSE_SCHEMA, [1])
    check_extraction_response(data['atomicExtractionExample']['output'], EXTRACTION_RESPONSE_SCHEMA, [1])
    g = data['groundingExample']
    check_response(g['output'], g['input'])
    assert packet_schema(g['input'])['properties']['results']['minItems'] == 1
    result = g['output']['results'][0]
    text = '\n'.join([
        'C1.scope\tscope\tin_scope\t-\tPlant anatomy fits the scope.',
        f'C1.subject\tconcept\texact\t{result["subject"]["term"]}\tMatches root.',
        f'C1.object\tconcept\texact\t{result["object"]["term"]}\tMatches root system.',
        f'C1.relation\trelation\texact\t{result["relation"]["term"]}\tMatches part of.',
    ])
    parsed = wire_rows.parse_rows(text, g['input'])
    check_response(parsed, g['input'])


@pytest.mark.parametrize('subject,relation,obj,qualifiers', [
    ('Treatment T', 'increased', 'root length', {'population': 'seedlings', 'metric': '12%'}),
    ('Group A', 'had higher', 'Trait Q', {'comparator': 'Group B'}),
    ('Compound Q deficiency', 'may reduce', 'root length', {'condition': 'under limited supply'}),
    ('Treatment T', 'did not significantly affect', 'root-to-shoot ratio', {}),
    ('Combined Treatment T and Treatment U', 'increased', 'Trait Q', {}),
])
def test_source_anchoring_keeps_atomic_fields_and_defining_meaning(tmp_path, subject, relation, obj, qualifiers):
    sentence = f'{subject} {relation} {obj}.'
    item = {'subject': subject, 'relation': relation, 'object': obj, 'qualifiers': qualifiers,
            'subject_anchor': subject, 'object_anchor': obj, 'evidence_sentence_ids': [1]}
    class Client:
        def chat_json(self, *args, **kwargs):
            answer = {'claims': [deepcopy(item)]}
            kwargs['response_check'](answer)
            return answer
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Abstract fixture'):
        claims, rejected, _ = extract_claims(pdf, Client(), 0, lambda *args: None,
            packets=[packet([row(1, text=sentence)])], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert not rejected and len(claims) == 1
    assert claims[0]['subject'] == subject and claims[0]['object'] == obj
    assert claims[0]['relation'] == relation and claims[0]['qualifiers'] == qualifiers
    assert claims[0]['source_evidence_span'] == sentence
