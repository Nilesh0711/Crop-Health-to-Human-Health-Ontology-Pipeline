"""Context is retained without converting shared classes into shared observations."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import URIRef
from rdflib.namespace import OWL

from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.ontology.registry import entity_registry
from backend.shared.ontology_browser import create_router
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.ontology_grounding.grounder import ground_claims, request_packet
from tests.backend.test_extraction_targets import packet, row
from tests.backend.test_ontology_grounding import Grounder, claim


def test_same_base_entities_with_different_organs_are_not_deduplicated(tmp_path):
    sentence = 'Treatment T may increase phosphorus content in roots and shoots under low supply.'
    items = [{'subject': 'Treatment T', 'relation': 'may increase', 'object': 'phosphorus content',
              'object_context': organ, 'qualifiers': {'condition': 'under low supply'},
              'evidence_sentence_ids': [1]} for organ in ('roots', 'shoots')]
    class Client:
        def chat_json(self, *args, **kwargs):
            value = {'claims': items}; kwargs['response_check'](value); return value
    pdf = Path(tmp_path) / 'source.pdf'; pdf.write_bytes(b'fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Synthetic fixture'):
        claims, rejected, _ = extract_claims(pdf, Client(), 0, lambda *args: None,
            packets=[packet([row(1, text=sentence)])], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert not rejected and len(claims) == 2
    assert claims[0]['candidate_id'] != claims[1]['candidate_id']
    assert [c['object_context'] for c in claims] == ['roots', 'shoots']
    assert all(c['relation'] == 'may increase' and c['source_evidence_span'] == sentence for c in claims)
    unique, removed = deduplicate_source_claims([*deepcopy(claims), deepcopy(claims[0])])
    assert len(unique) == 2 and removed == 1


def test_same_trait_comparison_requires_distinct_endpoint_contexts(tmp_path):
    sentence = 'Trait Q in Cultivar A is higher than Trait Q in Cultivar B.'
    good = {'subject': 'Trait Q', 'subject_context': 'Cultivar A', 'object': 'Trait Q',
            'object_context': 'Cultivar B', 'relation': 'is higher than', 'qualifiers': {}, 'evidence_sentence_ids': [1]}
    missing = {k: v for k, v in good.items() if not k.endswith('_context')}
    class Client:
        def chat_json(self, *args, **kwargs):
            result = {'claims': [good, missing]}; kwargs['response_check'](result); return result
    pdf = Path(tmp_path) / 'source.pdf'; pdf.write_bytes(b'fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Synthetic fixture'):
        claims, rejected, _ = extract_claims(pdf, Client(), 0, lambda *args: None,
            packets=[packet([row(1, text=sentence)])], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert len(claims) == 1 and rejected['identical_endpoints'] == 1
    assert claims[0]['subject_context'] == 'Cultivar A' and claims[0]['object_context'] == 'Cultivar B'
    assert claims[0]['source_evidence_span'] == sentence


def test_grounding_and_registry_keep_context_bearing_exact_matches_separate(tmp_path):
    reference = Catalog(RESOURCE_DIR)
    claims = [claim('a', relation='unmapped_predicate', object_context='potato roots, cultivar A'),
              claim('b', relation='unmapped_predicate', object_context='potato roots, cultivar B')]
    payload, _ = request_packet(claims, reference)
    assert payload['claims']['C1']['object_context'] == claims[0]['object_context']
    rows = ground_claims(claims, tmp_path, reference, Grounder())
    assert all(not r['assembly_eligible'] for r in rows)
    objects = [r['bindings']['object'] for r in rows]
    assert all(b['mapping'] == 'exact' for b in objects)
    assert objects[0]['canonical_iri'] == objects[1]['canonical_iri']
    assert objects[0]['entity_id'] != objects[1]['entity_id']
    entities = [e for e in entity_registry(rows) if any(o['role'] == 'object' for o in e['occurrences'])]
    assert len(entities) == 2 and all(not e['shared_concept'] for e in entities)
    assert {e['occurrences'][0]['endpoint_context'] for e in entities} == {c['object_context'] for c in claims}


def test_entity_filter_distinguishes_broader_classification_from_review(tmp_path, monkeypatch):
    from backend.shared import ontology_browser
    reference = Catalog(RESOURCE_DIR)
    rows = ground_claims([claim('a'), claim('b')], tmp_path, reference, Grounder())
    for r in rows:
        r['bindings']['subject']['mapping'] = 'broader'
    rows[0]['bindings']['object']['mapping'] = 'related'
    rows[1]['bindings']['object']['mapping'] = 'unresolved'
    rows[1]['bindings']['object']['iri'] = None
    state = {'catalog_signature': reference.signature, 'rules_signature': reference.rules_signature,
             'assertions': rows, 'entities': entity_registry(rows), 'counts': {}}
    monkeypatch.setattr(ontology_browser, 'catalog', lambda: reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path)
    app = FastAPI(); app.include_router(create_router(workspace, read_only=True,
        snapshot_loader=lambda path, default: state if path.name == 'snapshot.json' else default))
    with TestClient(app) as client:
        classified = client.get('/api/ontology/entities?status=classification').json()
        assert classified['total'] == 2
        assert all(e['mapping'] == 'broader' for e in classified['entities'])
        assert classified['status_counts']['classification'] == 2
        assert classified['status_counts']['review'] == 1
        assert client.get('/api/ontology/entities?status=review').json()['total'] == 1
        assert client.get('/api/ontology/entities?status=vocabulary_gap').json()['total'] == 1


def test_reference_extension_uses_explicit_sources_and_distinct_process_properties():
    reference = Catalog(RESOURCE_DIR)
    assert len(reference.profile['allowed_classes']) == 203
    assert len(reference.profile['allowed_object_properties']) == 10
    for identifier in ('RO_0000056', 'RO_0000057', 'RO_0002020', 'RO_0002233', 'RO_0002234'):
        term = reference.terms['http://purl.obolibrary.org/obo/' + identifier]
        assert term['origin'] == 'established_ontology' and term['definition'] and term['sources']
    assert 'process' in reference.terms['http://purl.obolibrary.org/obo/RO_0002020']['definition']
    assert all(not iri.startswith('urn:soil-crop-health:') for iri in reference.profile['allowed_object_properties'])
    assert reference.terms['http://purl.obolibrary.org/obo/RO_0000056']['label'] == 'participates in'


def test_coordination_and_surface_anchoring_extensions():
    from backend.stages.claim_extraction.coordination import coordinated_source_mention, prepositional_source_mention
    from backend.shared.text import source_surface_mention

    # 1. Gene coordination with semicolon
    gene_span = 'increased the relative expression of StPHT1;1 and StPHT2;1 in Lady Claire leaves'
    assert coordinated_source_mention(gene_span, 'relative expression of StPHT2;1', 'increases') == 'relative expression of StPHT1;1 and StPHT2;1'

    # 2. Plural head-noun coordination
    content_span = 'where shoot and root phosphorus contents increased by 13–25%'
    assert coordinated_source_mention(content_span, 'shoot phosphorus content', 'increases') == 'shoot and root phosphorus contents'
    assert source_surface_mention(content_span, 'root phosphorus content') == 'root phosphorus contents'

    # 3. Prepositional expression of
    exp_span = 'The slightly reduced expression of StPHT1;7 in Milva roots under P30'
    assert prepositional_source_mention(exp_span, 'StPHT1;7 expression') == 'expression of StPHT1;7'

    # 4. Parenthetical annotation gap
    dose_span = 'exposed to both extremely low (0 mg P L − 1) and high P levels'
    assert coordinated_source_mention(dose_span, 'extremely low P levels') == 'extremely low (0 mg P L − 1) and high P levels'

