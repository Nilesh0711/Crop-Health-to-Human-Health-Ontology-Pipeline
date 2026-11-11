"""Offline structural/logic tests, not a claim of scientific mapping accuracy."""
from copy import deepcopy
from types import SimpleNamespace
import json

import numpy as np
import pytest
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS, OWL
from fastapi.testclient import TestClient
from fastapi import FastAPI
from threading import RLock

from backend.ontology.catalog import Catalog, RESOURCE_DIR, LOCAL, rdf_graph
from backend.stages.ontology_grounding.grounder import ground_claims, request_packet, claim_signature
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from backend.stages.ontology_reasoning.engine import reason, validate_rule, TYPE
from backend.providers.structured_output import IncompleteResponseError, SchemaValidationError
from backend.pipelines.ontology import rebuild, export_rdf, DIRECTORY
from backend.shared.storage import read_jsonl, stable_hash
from backend.shared.artifacts import save_rows
from backend.shared.ontology_browser import create_router
from backend.stages.evaluation.grounding import export_sample, evaluate
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.inference import InferenceError
from backend.config.settings import Settings
from dataclasses import replace

NITRATE = 'http://purl.obolibrary.org/obo/CHEBI_17632'
ROOT = 'http://purl.obolibrary.org/obo/PO_0009005'


@pytest.fixture
def reference():
    reference = Catalog(RESOURCE_DIR)
    # Mechanical regression fixture also covers non-pilot terms. This does not
    # expand the production extraction/grounding selection.
    for iri in [NITRATE, 'http://purl.obolibrary.org/obo/PO_0025034',
                'http://purl.obolibrary.org/obo/GO_0048364', 'http://purl.obolibrary.org/obo/RO_0001025']:
        kind = 'allowed_classes' if reference.terms[iri]['kind'] == 'class' else 'allowed_object_properties'
        if iri not in reference.profile[kind]:
            reference.profile[kind].append(iri)
        reference.terms[iri]['selectable'] = True
    reference.signature = stable_hash(reference.signature + '|offline-mechanical-regression-fixture', 64)
    return reference


class Encoder:
    embedding_identity = {'model': 'offline-synthetic-vectors'}
    def encode_texts(self, texts):
        result = []
        for text in texts:
            vector = np.ones(32, dtype=float)
            for word in text.casefold().split():
                vector[int(stable_hash(word, 8), 16) % len(vector)] += 1
            result.append(vector / np.linalg.norm(vector))
        return result


class Grounder:
    model = 'offline-grounding-fixture'
    def __init__(self, *, denotation='concept', scope='in_scope', fail_at=None):
        self.denotation, self.scope, self.fail_at = denotation, scope, fail_at
        self.calls = []
        self.settings = SimpleNamespace(task_thinking={'pathway_assembly': False}, pathway_assembly_input_characters=140000,
                                        ontology_grounding_output_format='json')
    def chat_json(self, system, user, **kwargs):
        data = json.loads(user)
        self.calls.append(data)
        if len(self.calls) == self.fail_at:
            raise RuntimeError('Simulated interrupted grounding')
        answer = {'results': []}
        for key, claim in data['claims'].items():
            row = {'claim_id': key, 'scope': self.scope, 'scope_reason': 'Synthetic scope fixture, not a scientific judgement.',
                   'subject_denotation': self.denotation, 'object_denotation': self.denotation}
            for role in ('subject', 'object'):
                candidates = claim.get(role + '_candidates', data['terms'])
                choice = next((i for i in candidates if claim[role].casefold() ==
                    data['terms'][i]['label'].casefold()), None) or \
                         next((i for i in candidates if claim[role].casefold() in
                    {s.casefold() for s in data['terms'][i].get('exact_synonyms', [])}), None)
                row[role] = {'term': choice, 'mapping': 'exact' if choice else 'unresolved',
                             'reason': 'Synthetic exact-name alignment.'}
            relation = next((i for i, t in data['relations'].items() if t['label'] == claim['relation']), None)
            row['relation'] = {'term': relation, 'mapping': 'exact' if relation else 'unresolved', 'reason': 'Fixture relation.'}
            answer['results'].append(row)
        kwargs['response_check'](answer)
        return answer


def claim(identifier='one', **extra):
    return {'candidate_id': identifier, 'subject': 'nitrate', 'relation': 'located in', 'object': 'root',
            'subject_type': 'Unresolved', 'object_type': 'PlantProcess', 'schema_error': None,
            'source_evidence_span': 'Synthetic example: nitrate affects the root under Condition Q.',
            'source_local_context': 'Shared synthetic study context.', 'source_page': 1,
            'source_references': [{'title': 'Synthetic reference fixture'}],
            'certainty': 'conditional', 'qualifiers': {'condition': 'Condition Q'}, **extra}


def test_frozen_reference_contains_real_definitions_and_separate_chemistry_property(reference):
    assert reference.terms[NITRATE]['label'] == 'nitrate'
    assert 'oxoanion' in reference.terms[NITRATE]['definition']
    assert reference.terms['http://purl.obolibrary.org/obo/TO_0020093']['label'] == 'nitrogen content'
    assert reference.terms[NITRATE]['origin'] == 'established_ontology'
    assert reference.terms[LOCAL + 'numericValue']['kind'] == 'data_property'


def test_only_established_scientific_terms_are_selectable(reference):
    assert all(not i.startswith(LOCAL) for i in reference.profile['allowed_classes'])
    assert all(not i.startswith(LOCAL) for i in reference.profile['allowed_object_properties'])


@pytest.mark.parametrize('data', [b'<!DOCTYPE root><root/>', b'<!ENTITY x "value"><root/>'])
def test_xml_entity_expansion_is_not_an_import_feature(data):
    with pytest.raises(ValueError, match='DTD|entity'):
        rdf_graph(data, 'xml')


def test_hybrid_candidates_are_signals_not_proof(reference):
    index = ConceptIndex(reference, Encoder())
    hits = index.retrieve('nitrate', 'NO3(-)', 'A soil nutrient observation.', top_k=len(index.ids))
    term = next(t for t in hits if t['iri'] == NITRATE)
    assert {'exact_label_or_synonym', 'lexical', 'semantic_name', 'semantic_context'} <= set(term['retrieval_signals'])
    assert term['retrieval_is_identity_proof'] is False


def test_unknown_candidate_is_rejected_before_it_can_become_identity(reference):
    packet, check = request_packet([claim()], ConceptIndex(reference))
    client = Grounder()
    answer = client.chat_json('', json.dumps(packet), response_check=lambda _: None)
    answer['results'][0]['subject']['term'] = 'invented-term'
    with pytest.raises((IncompleteResponseError, SchemaValidationError)):
        check(answer)


def test_extra_entity_field_on_relation_is_regenerated_by_the_same_primary_not_dropped_locally(tmp_path, reference, monkeypatch):
    packet, _ = request_packet([claim()], ConceptIndex(reference))
    valid = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    invalid = deepcopy(valid)
    invalid['results'][0]['relation']['denotation'] = 'concept'
    calls, metrics = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly', on_request=metrics.append)
    def chat(model, system, user, schema, output_limit):
        calls.append((model, system, user, schema))
        return {'content': json.dumps(invalid if len(calls) == 1 else valid), 'finish_reason': 'stop',
                'usage': {}, 'response_format': 'json_schema', 'provider_request_id': f'test-{len(calls)}'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    rows = ground_claims([claim()], tmp_path, reference, client)
    assert len(calls) == 2 and {c[0] for c in calls} == {Settings().pathway_assembly_model}
    assert calls[0][2] == calls[1][2] and 'NEVER put denotation inside any mapping object' in calls[0][1]
    assert metrics[0]['status'] == 'schema_error' and metrics[1]['recovery_completed']
    assert all(m['stage'] == 'ontology_grounding' for m in metrics)
    assert rows[0]['provider_request_id'] == 'test-2'
    assert rows[0]['model'] == client.model and rows[0]['model_provenance'] == 'request_metrics'
    assert 'denotation' not in rows[0]['bindings']['relation']


def test_invented_placeholder_key_is_still_rejected(reference):
    packet, check = request_packet([claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    answer['C1_placeholder'] = deepcopy(answer['results'][0])
    with pytest.raises(SchemaValidationError):
        check(answer)


def test_grounding_refuses_misattributed_model_response_without_saving_it(tmp_path, reference):
    client = Grounder()
    client.last_request_metrics = {'request_model': 'different-model'}
    with pytest.raises(ValueError, match='configured primary'):
        ground_claims([claim()], tmp_path, reference, client)
    assert not (tmp_path / 'ontology_grounding.jsonl').exists()


@pytest.mark.parametrize('wire_format', ['tsv', 'json'])
def test_grounding_timeout_is_separate_and_never_substitutes_a_model(tmp_path, reference, monkeypatch, wire_format):
    settings = replace(Settings(), request_timeout_seconds=300, ontology_grounding_timeout_seconds=600,
                       ontology_grounding_output_format=wire_format)
    metrics, calls = [], []
    client = OpenCodeClient(settings, task='pathway_assembly', on_request=metrics.append)
    assert client.settings.request_timeout_seconds == 600
    assert OpenCodeClient(settings, task='source').settings.request_timeout_seconds == 300
    assert not hasattr(settings, 'fallback_models')
    def timeout(*args):
        calls.append(args)
        raise InferenceError('Request timed out after 600s', 'timeout')
    monkeypatch.setattr(client._inference, 'chat_text' if wire_format == 'tsv' else 'chat', timeout)
    with pytest.raises(LLMServiceError, match='timed out'):
        ground_claims([claim()], tmp_path, reference, client)
    assert len(calls) == 1 and client.model == settings.pathway_assembly_model
    assert metrics[0]['timeout_seconds'] == 600 and metrics[0]['status'] == 'timeout'
    assert not (tmp_path / 'ontology_grounding.jsonl').exists()
    with pytest.raises(ValueError, match='timeouts'):
        replace(settings, ontology_grounding_timeout_seconds=0)


def test_generic_concepts_share_identity_without_combining_evidence_or_conditions(tmp_path, reference):
    claims = [claim('a'), claim('b', subject='NITRATE', qualifiers={'condition': 'Condition R'})]
    original = deepcopy(claims)
    rows = ground_claims(claims, tmp_path, reference, Grounder(), encoder=Encoder())
    assert rows[0]['bindings']['subject']['entity_id'] != rows[1]['bindings']['subject']['entity_id']
    assert rows[0]['bindings']['subject']['occurrence_id'] != rows[1]['bindings']['subject']['occurrence_id']
    assert claims == original and rows[0]['original_claim']['qualifiers'] != rows[1]['original_claim']['qualifiers']
    assert rows[0]['original_claim']['source_references'] == claims[0]['source_references']


def test_related_synonyms_are_not_lexical_equivalence_proof(reference):
    reference.terms[NITRATE]['synonyms'].append('synthetic related phrase')
    hits = ConceptIndex(reference).retrieve('synthetic related phrase', '', '', top_k=30)
    nitrate = next(t for t in hits if t['iri'] == NITRATE)
    assert 'non_equivalence_synonym' in nitrate['retrieval_signals']
    assert 'exact_label_or_synonym' not in nitrate['retrieval_signals']


@pytest.mark.parametrize('denotation', ['particular', 'state'])
def test_shared_class_does_not_merge_distinct_samples_or_states(tmp_path, reference, denotation):
    rows = ground_claims([claim('a'), claim('b')], tmp_path, reference, Grounder(denotation=denotation))
    assert rows[0]['bindings']['subject']['iri'] == rows[1]['bindings']['subject']['iri']
    assert rows[0]['bindings']['subject']['entity_id'] != rows[1]['bindings']['subject']['entity_id']


def test_shared_ontology_junction_connects_branches_without_comparing_whole_paths(tmp_path, reference, monkeypatch):
    monkeypatch.setattr('backend.pipelines.ontology.POST_GROUNDING_STAGES_PAUSED', False)
    output = tmp_path / 'paper'; output.mkdir()
    claims = [claim('a', qualifiers={}, source_evidence_sentence_numbers=[1]), claim('b', subject='root', object='root length', qualifiers={}, source_evidence_sentence_numbers=[1]), claim('c', subject='root', object='leaf', qualifiers={}, source_evidence_sentence_numbers=[1])]
    save_rows(output / 'candidates.jsonl', claims)
    client = Grounder()
    ground_claims(claims, output, reference, client, batch_size=3)
    state, _ = rebuild(tmp_path, reference)
    edges = {e['candidate_id']: e for e in state['graph']['edges']}
    assert edges['a']['target'] == edges['b']['source'] == edges['c']['source']
    assert len(client.calls) == 1 and len(edges) == 3
    assert state['causal_chain_validated'] is False


def test_explicit_named_equivalence_is_distinct_from_parent_membership(reference):
    alias = 'urn:test:nitrate-equivalent'
    reference.terms[alias] = {**reference.terms[NITRATE], 'iri': alias}
    reference.graph.add((URIRef(alias), OWL.equivalentClass, URIRef(NITRATE)))
    assert reference.canonical_equivalent(alias) == reference.canonical_equivalent(NITRATE)
    parent = reference.terms[NITRATE]['parents'][0]
    assert reference.canonical_equivalent(NITRATE) != reference.canonical_equivalent(parent)


def test_ontology_grounding_does_not_force_unknown_whole_meanings(tmp_path, reference):
    rows = ground_claims([claim(subject='soil nitrate concentration')], tmp_path, reference, Grounder())
    assert not rows[0]['assembly_eligible'] and rows[0]['bindings']['subject']['mapping'] == 'unresolved'
    assert rows[0]['original_claim']['subject'] == 'soil nitrate concentration'


def test_cache_signature_reuses_same_inputs_and_invalidates_changed_conditions(tmp_path, reference):
    client = Grounder()
    ground_claims([claim()], tmp_path, reference, client)
    ground_claims([claim(source_validation={'decision': 'needs_review'})], tmp_path, reference, client)
    assert len(client.calls) == 1
    ground_claims([claim(qualifiers={'condition': 'changed'})], tmp_path, reference, client)
    assert len(client.calls) == 2


def test_interrupted_batch_keeps_completed_grounding_and_resumes_only_remaining(tmp_path, reference):
    claims = [claim('a'), claim('b')]
    client = Grounder(fail_at=2)
    with pytest.raises(RuntimeError, match='interrupted'):
        ground_claims(claims, tmp_path, reference, client, batch_size=1)
    assert [r['candidate_id'] for r in read_jsonl(tmp_path / 'ontology_grounding.jsonl')] == ['a']
    resumed = Grounder()
    assert len(ground_claims(claims, tmp_path, reference, resumed, batch_size=1)) == 2
    assert len(resumed.calls) == 1


def test_named_axiom_inference_has_existing_premises_and_no_generic_causal_transitivity(reference):
    a, b, c = map(URIRef, ['urn:example:A', 'urn:example:B', 'urn:example:C'])
    reference.graph.add((a, RDFS.subClassOf, b)); reference.graph.add((b, RDFS.subClassOf, c))
    result = reason(reference, [])
    inferred = next(f for f in result['facts'] if f['subject'] == str(a) and f['object'] == str(c) and f['predicate'] == str(RDFS.subClassOf))
    facts = {f['id'] for f in result['facts']}
    assert inferred['rule'] == 'hierarchy_transitivity' and set(inferred['premises']) <= facts
    assert result['coverage']['complete_owl_consistency'] is False
    assert not any(f['predicate'] == LOCAL + 'causes' for f in result['facts'])


def test_unreviewed_biological_rule_cannot_run(reference):
    rule = {'id': 'unsupported-cause', 'body': [['?a', LOCAL + 'causes', '?b']], 'head': ['?a', LOCAL + 'causes', '?b']}
    with pytest.raises(ValueError, match='review'):
        validate_rule(rule, reference)


def test_declared_disjointness_reports_an_unsatisfiable_class(reference):
    a, b, c = map(URIRef, ['urn:test:ClassA', 'urn:test:ClassB', 'urn:test:ClassC'])
    reference.graph.add((a, OWL.disjointWith, b))
    reference.graph.add((c, RDFS.subClassOf, a)); reference.graph.add((c, RDFS.subClassOf, b))
    result = reason(reference, [])
    assert any(issue.get('class') == str(c) and issue['kind'] == 'unsatisfiable_class_in_supported_fragment' for issue in result['issues'])


def test_reviewed_rules_cannot_join_different_study_conditions(tmp_path, reference):
    rows = ground_claims([claim('a'), claim('b', subject='root', object='root length',
        qualifiers={'condition': 'different'})], tmp_path, reference, Grounder())
    # Test-only supplied identity: isolate the reasoner's condition guard from registry sharing.
    rows[1]['bindings']['subject']['entity_id'] = rows[0]['bindings']['object']['entity_id']
    reference.profile['biological_rules'] = [{'id': 'synthetic-reviewed-rule', 'body': [
        ['?a', 'http://purl.obolibrary.org/obo/RO_0001025', '?b'], ['?b', 'http://purl.obolibrary.org/obo/RO_0001025', '?c']],
        'head': ['?a', 'http://purl.obolibrary.org/obo/BFO_0000050', '?c'], 'reviewed_by': 'Offline test fixture',
        'review_note': 'Mechanical test, not a real biological rule.', 'evidence_refs': ['urn:test:review'],
        'same_context': True, 'enabled': True}]
    assert not any(f['rule'] == 'synthetic-reviewed-rule' for f in reason(reference, rows)['facts'])
    rows[1]['original_claim']['qualifiers'] = rows[0]['original_claim']['qualifiers']
    inferred = [f for f in reason(reference, rows)['facts'] if f['rule'] == 'synthetic-reviewed-rule']
    assert len(inferred) == 1 and inferred[0]['scientific_status'].startswith('conditional_inference')


def test_rebuild_excludes_changed_claims_and_rdf_export_reifies_evidence(tmp_path, reference, monkeypatch):
    monkeypatch.setattr('backend.pipelines.ontology.POST_GROUNDING_STAGES_PAUSED', False)
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim()])
    ground_claims([claim()], output, reference, Grounder())
    state, _ = rebuild(tmp_path, reference)
    assert state['counts']['mapped_claims'] == 1
    graph = rdf_graph((tmp_path / DIRECTORY / 'assertions.ttl').read_bytes(), 'turtle')
    assert not list(graph.triples((None, URIRef('http://purl.obolibrary.org/obo/RO_0001025'), None)))
    assert list(graph.triples((None, RDF.predicate, URIRef('http://purl.obolibrary.org/obo/RO_0001025'))))
    save_rows(output / 'candidates.jsonl', [claim(qualifiers={'condition': 'changed'})])
    state, _ = rebuild(tmp_path, reference)
    assert not state['assertions'] and len(state['stale_assertions']) == 1


def test_old_automatic_model_provenance_requires_regrounding_but_manual_reviews_are_preserved(tmp_path, reference):
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim()])
    row = ground_claims([claim()], output, reference, Grounder())[0]
    row['policy'] = 'ontology-grounding-v1'
    save_rows(output / 'ontology_grounding.jsonl', [row])
    state, _ = rebuild(tmp_path, reference)
    assert not state['assertions'] and len(state['stale_assertions']) == 1
    row['mapping_review'] = 'manually_reviewed'
    save_rows(output / 'ontology_grounding.jsonl', [row])
    state, _ = rebuild(tmp_path, reference)
    assert not state['assertions'] and len(state['stale_assertions']) == 1  # Review cannot bypass changed scientific policy.


def test_benchmark_does_not_invent_gold_or_overwrite_labels(tmp_path, reference):
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim()])
    ground_claims([claim()], output, reference, Grounder())
    rebuild(tmp_path, reference)
    dest = tmp_path / 'benchmark.jsonl'
    export_sample(tmp_path, dest)
    report = evaluate(dest)
    assert report['reviewed'] == 0 and report['mapping_accuracy'] is None
    with pytest.raises(ValueError, match='already exists'):
        export_sample(tmp_path, dest)


def test_browser_is_paginated_and_mapping_review_does_not_validate_science(tmp_path, reference, monkeypatch):
    from backend.shared import ontology_browser as browser
    monkeypatch.setattr(browser, 'catalog', lambda: reference)
    output = tmp_path / 'paper'; output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim()])
    ground_claims([claim()], output, reference, Grounder())
    rebuild(tmp_path, reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={},
        _pdf_path=lambda _: 'paper.pdf', _output_dir=lambda _: output, rebuild_ontology=rebuild)
    app = FastAPI(); app.include_router(create_router(workspace))
    with TestClient(app) as client:
        assert len(client.get('/api/ontology/terms?limit=2').json()['terms']) == 2
        row = client.get('/api/ontology/assertions').json()['assertions'][0]
        review = {'role': 'subject', 'iri': NITRATE, 'mapping': 'exact', 'note': 'Reviewed the supplied definition.', 'expected_revision': row['revision']}
        response = client.post('/api/ontology/pdfs/p/claims/one/review', json=review)
        assert response.status_code == 200 and 'not scientific validation' in response.json()['message']
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=review).status_code == 409
        assert read_jsonl(output / 'ontology_grounding.jsonl')[0]['scientific_validation'] == 'not_assessed'


def test_browser_full_names_keep_original_revision_and_literal_quote(tmp_path, reference, monkeypatch):
    from backend.shared import ontology_browser as browser
    monkeypatch.setattr(browser, 'catalog', lambda: reference)
    output = tmp_path / 'paper'; output.mkdir()
    candidate = claim(subject='DM', source_evidence_span='Dry matter (DM) increased.')
    save_rows(output / 'candidates.jsonl', [candidate])
    ground_claims([candidate], output, reference, Grounder())
    rebuild(tmp_path, reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={},
        _pdf_path=lambda _: 'paper.pdf', _output_dir=lambda _: output, rebuild_ontology=rebuild)
    app = FastAPI(); app.include_router(create_router(workspace))
    with TestClient(app) as client:
        row = client.get('/api/ontology/assertions').json()['assertions'][0]
        assert row['original_claim']['subject'] == 'Dry matter'
        assert row['original_claim']['source_evidence_span'] == candidate['source_evidence_span']
        raw = read_jsonl(output / 'ontology_grounding.jsonl')[0]
        assert raw['original_claim']['subject'] == 'DM'
        review = {'role': 'subject', 'iri': NITRATE, 'mapping': 'exact',
                  'note': 'Testing revision compatibility only.', 'expected_revision': row['revision']}
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=review).status_code == 200


def test_term_endpoint_returns_extended_class_hierarchy_and_children(tmp_path, reference, monkeypatch):
    from backend.shared import ontology_browser as browser
    monkeypatch.setattr(browser, 'catalog', lambda: reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={},
        _pdf_path=lambda _: 'paper.pdf', _output_dir=lambda _: tmp_path, rebuild_ontology=lambda *_: None)
    app = FastAPI(); app.include_router(create_router(workspace))
    with TestClient(app) as client:
        # Check an existing class in reference
        term_res = client.get(f'/api/ontology/term?iri={NITRATE}')
        assert term_res.status_code == 200
        data = term_res.json()
        assert 'ancestors' in data
        assert 'lineage_paths' in data
        assert isinstance(data['lineage_paths'], list)
        assert len(data['lineage_paths']) >= 1
        assert 'children' in data
        assert 'child_count' in data
        assert 'has_children' in data
        # Target node should be the last step of lineage paths
        assert data['lineage_paths'][0][-1]['iri'] == NITRATE
