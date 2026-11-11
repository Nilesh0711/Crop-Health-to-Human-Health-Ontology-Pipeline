"""Generic fidelity/provenance checks; examples are not runtime mapping rules."""
from copy import deepcopy
import json
from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from rdflib import Graph, URIRef

from backend.domain.abbreviations import abbreviation_source_anchor, packet_definitions
from backend.domain.extraction_review import extraction_warnings, refresh_eligibility
from backend.ontology.catalog import catalog, RESOURCE_DIR
from backend.ontology.registry import entity_registry
from backend.pipelines.ontology import rebuild, run_grounding
from backend.providers.structured_output import SchemaValidationError
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl
from backend.stages.claim_extraction.targeting import check_target_evidence
from backend.stages.claim_extraction.extractor import _bind_claim_evidence, _proposed_candidate, _ExtractionResults
from backend.stages.ontology_grounding.grouping import evidence_groups, flag_consistency
from backend.stages.ontology_grounding.grounder import ground_claims, request_packet
from tests.backend.test_ontology_grounding import Grounder, claim
from tests.backend.test_extraction_targets import packet, row


def test_only_upstream_scientific_terms_and_annotations():
    ref = catalog()
    assert len(ref.profile['allowed_object_properties']) == 19
    assert len(ref.profile['allowed_classes']) == 211
    assert all(ref.terms[i]['origin'] == 'established_ontology'
               for i in ref.profile['allowed_classes'] + ref.profile['allowed_object_properties'])
    local = Graph().parse(RESOURCE_DIR / 'application.ttl', format='turtle')
    assert not any(str(s).startswith('http://purl.obolibrary.org/obo/') for s in local.subjects())
    assert not any(str(s) == 'urn:soil-crop-health:increases' for s in local.subjects())
    payload, _ = request_packet([claim()], ref)
    assert all('parents' in term for term in payload['terms'].values())
    assert all('domain' in term and 'range' in term for term in payload['relations'].values())


def test_supporting_context_must_include_a_target_and_cannot_invent_ids():
    value = {'claims': [{'evidence_sentence_ids': [1, 3]}]}
    check_target_evidence(value, [1, 2, 3], [3])
    for numbers in [[1], [3, 4], [3, 1]]:
        with pytest.raises(SchemaValidationError):
            check_target_evidence({'claims': [{'evidence_sentence_ids': numbers}]}, [1, 2, 3], [3])


def test_evidence_binding_preserves_target_and_supporting_context_roles():
    source = packet([row(1, text='Treatment T was applied.'), row(2, text='It increases Trait Q.')])
    source.update(target_id='target', target_sentence_numbers=[2], reading_context_sentence_numbers=[1])
    item = {'subject': 'Treatment T', 'relation': 'increases', 'object': 'Trait Q',
            'qualifiers': {}, 'evidence_sentence_ids': [1, 2]}
    evidence, _ = _bind_claim_evidence(item, _proposed_candidate(item), source, 'target_context', _ExtractionResults())
    assert evidence['target_sentence_numbers'] == [2]
    assert evidence['supporting_context_sentence_numbers'] == [1]
    item['evidence_sentence_ids'] = [1]
    state = _ExtractionResults()
    assert _bind_claim_evidence(item, _proposed_candidate(item), source, 'target_context', state) is None
    assert state.rejected['context_only_finding'] == 1


def test_document_defined_abbreviation_matching_preserves_literal_span():
    packets = [{'packet_number': 1, 'sentences': [{'text': 'Dry matter (DM) was recorded.', 'page': 1}]}]
    glossary = packet_definitions(packets, 'document-a')
    match = abbreviation_source_anchor('dry matter', 'DM increased.', glossary, 'document-a')
    assert match and match[0] == 'DM' and match[1][0]['quote'] == 'Dry matter (DM)'
    assert abbreviation_source_anchor('dry matter', 'DM increased.', glossary, 'document-b') is None
    assert abbreviation_source_anchor('dry weight', 'DM increased.', glossary, 'document-a') is None
    packets[0]['sentences'].append({'text': 'Dry mass (DM) was measured.', 'page': 2})
    assert abbreviation_source_anchor('dry matter', 'DM increased.', packet_definitions(packets, 'document-a'), 'document-a') is None
    unit_glossary = packet_definitions([{'packet_number': 1, 'sentences': [{'text': 'Cultivar (C) was selected.', 'page': 1}]}], 'document-a')
    assert abbreviation_source_anchor('Cultivar', 'Temperature was 20°C.', unit_glossary, 'document-a') is None


@pytest.mark.parametrize(('sentence', 'subject', 'relation', 'obj', 'code'), [
    ('Plants cannot acquire Compound T.', 'Plants', 'acquires', 'Compound T', 'possible_lost_negation'),
    ('Only one out of 22 traits increased under Treatment T.', 'Treatment T', 'increases', 'traits', 'possible_generalized_subset'),
    ('Limited availability of Compound T reduces Trait Q.', 'Compound T', 'reduces', 'Trait Q', 'possible_lost_defining_state'),
])
def test_review_warnings_are_non_destructive(sentence, subject, relation, obj, code):
    c = claim(subject=subject, relation=relation, object=obj, source_evidence_span=sentence)
    original = deepcopy(c)
    assert code in {w['code'] for w in extraction_warnings(c)}
    assert c == original


def test_sentence_level_review_flags_unkept_negation_and_accepts_preserved_restriction():
    c = claim(subject='Treatment T', object='Trait Q', relation='increases', qualifiers={},
              source_evidence_span='Treatment T increased Trait Q as well as Trait R, though this increase was not significant.')
    assert 'possible_lost_negation' in {w['code'] for w in extraction_warnings(c)}
    c['source_evidence_span'] = 'Treatment T increases Trait Q, but the increase was not statistically significant.'
    c['qualifiers'] = {'condition': 'not statistically significant'}
    assert extraction_warnings(c) == []


def test_groups_never_fill_with_unrelated_evidence_and_are_stable():
    same = {'source_document_id': 'd', 'source_packet_number': 1, 'source_evidence_sentence_numbers': [2],
            'source_evidence_span': 'Original shared sentence.'}
    claims = [claim(str(i), **same) for i in range(4)] + [claim('other')]
    groups = evidence_groups(claims, 3)
    assert [len(g) for g in groups] == [3, 1, 1]
    assert [[c['candidate_id'] for c in g] for g in groups] == [[c['candidate_id'] for c in g] for g in evidence_groups(list(reversed(claims[:4])) + claims[4:], 3)]


def test_held_grounding_and_consistency_decisions_remain_separate(tmp_path):
    ref = catalog()
    c = claim('a', subject='nitrate', relation='located in', qualifiers={},
              source_evidence_span='Roots cannot acquire nitrate.')
    # Use matching literal anchors for a real warning, rather than a word elsewhere.
    c.update(subject='Roots', object='nitrate', relation='acquires')
    rows = ground_claims([c], tmp_path, ref, Grounder())
    assert rows[0]['extraction_review_status'] == 'held'
    assert not rows[0]['assembly_eligible']
    assert all(b['entity_id'].startswith('mention-') for role, b in rows[0]['bindings'].items() if role != 'relation')
    rows[0]['extraction_review'] = {'status': 'accepted', 'claim_signature': rows[0]['claim_signature']}
    refresh_eligibility(rows[0])
    assert rows[0]['extraction_review_status'] == 'accepted'
    rows[0]['claim_signature'] = 'changed'
    refresh_eligibility(rows[0])
    assert rows[0]['extraction_review_status'] == 'held'


def test_sibling_group_resume_and_changed_member_invalidates_group(tmp_path):
    ref = catalog()
    same = {'source_document_id': 'd', 'source_packet_number': 1, 'source_evidence_sentence_numbers': [1]}
    candidates = [claim('a', **same), claim('b', **same)]
    first = Grounder()
    rows = ground_claims(candidates, tmp_path, ref, first, batch_size=3)
    assert len(first.calls) == 1 and len(first.calls[0]['claims']) == 2
    second = Grounder()
    assert ground_claims(candidates, tmp_path, ref, second, batch_size=3) == rows
    assert second.calls == []
    candidates[1]['object_context'] = 'different bearer'
    third = Grounder()
    ground_claims(candidates, tmp_path, ref, third, batch_size=3)
    assert len(third.calls) == 1 and len(third.calls[0]['claims']) == 2


def test_changing_a_sibling_makes_the_entire_saved_group_stale(tmp_path):
    ref = catalog(); output = tmp_path / 'paper'; output.mkdir()
    same = {'source_document_id': 'd', 'source_packet_number': 1, 'source_evidence_sentence_numbers': [1]}
    candidates = [claim('a', **same), claim('b', **same)]
    save_rows(output / 'candidates.jsonl', candidates)
    ground_claims(candidates, output, ref, Grounder())
    assert len(rebuild(tmp_path, ref)[0]['assertions']) == 2
    candidates[1]['object_context'] = 'a different bearer'
    save_rows(output / 'candidates.jsonl', candidates)
    state, _ = rebuild(tmp_path, ref)
    assert not state['assertions'] and len(state['stale_assertions']) == 2


def test_consistency_conflicts_hold_without_overwriting(tmp_path):
    ref = catalog()
    same = {'source_document_id': 'd', 'source_packet_number': 1, 'source_evidence_sentence_numbers': [1]}
    rows = ground_claims([claim('a', **same), claim('b', **same)], tmp_path, ref, Grounder())
    rows[1]['bindings']['subject']['mapping'] = 'broader'
    original = deepcopy(rows[1]['bindings']['subject'])
    flag_consistency(rows)
    assert all(r['mapping_consistency_warnings'] for r in rows)
    assert all(not r['assembly_eligible'] for r in rows)
    assert rows[1]['bindings']['subject']['mapping'] == original['mapping']


def test_extraction_review_api_revision_and_read_only(tmp_path, monkeypatch):
    from backend.shared import ontology_browser as browser
    ref = catalog(); monkeypatch.setattr(browser, 'catalog', lambda: ref)
    output = tmp_path / 'paper'; output.mkdir()
    c = claim(subject='plants', object='nitrate', relation='absorbs', source_evidence_span='Plants cannot absorb nitrate.')
    save_rows(output / 'candidates.jsonl', [c])
    ground_claims([c], output, ref, Grounder())
    rebuild(tmp_path, ref)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={},
        _pdf_path=lambda _: 'paper.pdf', _output_dir=lambda _: output, rebuild_ontology=rebuild)
    app = FastAPI(); app.include_router(browser.create_router(workspace))
    with TestClient(app) as client:
        row = client.get('/api/ontology/assertions').json()['assertions'][0]
        decision = {'role': 'extraction', 'extraction_status': 'accepted', 'note': 'Reviewed against original text.', 'expected_revision': row['revision']}
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=decision).status_code == 200
        assert client.post('/api/ontology/pdfs/p/claims/one/review', json=decision).status_code == 409
        saved = read_jsonl(output / 'ontology_grounding.jsonl')[0]
        assert saved['extraction_review_status'] == 'accepted'
        assert saved['scientific_validation'] == 'not_assessed'
    app = FastAPI(); app.include_router(browser.create_router(workspace, read_only=True))
    assert TestClient(app).post('/api/ontology/pdfs/p/claims/one/review', json=decision).status_code == 403


def test_document_counts_do_not_include_other_documents(tmp_path):
    ref = catalog(); settings = SimpleNamespace(ontology_grounding_timeout_seconds=600,
        ontology_grounding_batch_size=3, ontology_grounding_workers=1, ontology_grounding_output_format='json')
    for name in ['first', 'second']:
        output = tmp_path / name; output.mkdir()
        candidates = [claim(name, qualifiers={})]
        save_rows(output / 'candidates.jsonl', candidates)
        _, summary = run_grounding(tmp_path, output, candidates, Grounder(), settings, lambda *_: None, reference=ref)
    assert summary['grounded_mentions'] == 2
    assert summary['global_registry_counts']['grounded_mentions'] == 4
    assert len(entity_registry(read_jsonl(output / 'ontology_grounding.jsonl'))) == 2
