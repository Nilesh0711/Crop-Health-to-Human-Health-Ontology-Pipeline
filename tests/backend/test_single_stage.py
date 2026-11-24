import json
import time
from contextlib import closing
from hashlib import sha256
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.single_stage import create_router
from backend.config.settings import Settings
from backend.domain.abbreviations import relevant_definitions, resolved_aliases
from backend.evaluation import single_stage
from backend.evaluation.extract_then_ground import (
    POLICY as EXTRACT_THEN_GROUND_POLICY,
    _spans_separated_by_clause_boundary,
    extraction_request,
    ground_extracted,
    load_extraction_schema,
    predicate_evidence_warnings,
    resolve_endpoint,
    resolve_relation,
    verify_anchors_and_evidence,
)
from backend.evaluation.ontology_lookup import endpoint_candidates, open_lookup, passage_candidates, verify_narrow_synonym_in_owl
from backend.ontology.catalog import catalog
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError


def test_single_stage_request_keeps_local_evidence_and_grounds_without_a_second_call(tmp_path, monkeypatch):
    strings = [
        'Earlier context concerned field trials.',
        'This was conducted in Dalhart (Dalhart 2020; Springlake 2020) (Smith et al., 2021).',
        'The root is part of the root system [1, 2].',
        'A root is part of a root system.',
        'This sentence is outside the neighbor window.',
    ]
    target = {'target_id': 'target-a', 'target_sentence_numbers': [3],
              'sentences': [{'text': text, 'page': 1, 'section_heading': 'Results'} for text in strings]}
    following_target = {**target, 'target_id': 'target-b', 'target_sentence_numbers': [4]}
    packet_stubs = [target, following_target]
    monkeypatch.setattr(single_stage, 'iter_pdf_context_packets', lambda pdf: iter(packet_stubs))

    def prepare_targets(packets, *args, **kwargs):
        assert list(packets) == packet_stubs
        assert list(packets) == packet_stubs
        return packets

    monkeypatch.setattr(single_stage, 'build_extraction_targets', prepare_targets)
    production = tmp_path / 'ontology' / 'paper' / 'candidates.jsonl'
    production.parent.mkdir(parents=True)
    production.write_text('original', encoding='utf-8')
    index = tmp_path / 'analysis' / 'ontology_full_releases'
    index.mkdir(parents=True)
    owl = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0009005"><rdfs:label>root</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0025025"><rdfs:label>root system</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_41510"><rdfs:label>H2PO4-</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000227"><rdfs:label>root length</rdfs:label></owl:Class>
         <owl:Class rdf:about="https://cropontology.org/rdf/CO_330:0000762"><rdfs:label>Root length</rdfs:label></owl:Class>
         </rdf:RDF>"""
    (index / 'sample.owl').write_bytes(owl)
    (index / 'inventory.json').write_text(json.dumps({
        'fixture': {'file': 'sample.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'fixture')
    calls = []
    settings_seen = []

    class Model:
        def __init__(self, settings, **kwargs):
            settings_seen.append(settings)

        def chat_json(self, system, passage, **options):
            calls.append(passage)
            assert 'Ontology identifiers are resolved later' in system
            assert 'Example 1' in system and 'Example 2' in system
            assert 'PO_0009005' not in system
            if len(calls) == 1:
                assert 'Dalhart 2020; Springlake 2020' in passage and 'Smith' not in passage and '[1, 2]' not in passage
                assert 'Earlier context' not in passage and 'outside the neighbor window' not in passage
            sid = 3 if len(calls) == 1 else 4
            claim = {
                'subject': 'root',
                'subject_type': 'plant part',
                'relation': 'is part of',
                'object': 'root system',
                'object_type': 'plant part',
                'assertion_status': 'affirmed',
                'qualifiers': {
                    'crop_or_species': None, 'population': None, 'condition': None,
                    'comparator': None, 'metric': None,
                },
                'support_sentence_ids': [],
                'subject_anchor': {'sentence_id': sid, 'quote': 'root'},
                'relation_anchor': {'sentence_id': sid, 'quote': 'is part of'},
                'object_anchor': {'sentence_id': sid, 'quote': 'root system'},
            }
            neighbor_only = {
                **claim,
                'support_sentence_ids': [sid],
                'relation_anchor': {'sentence_id': 4 if sid == 3 else 3, 'quote': 'is part of'},
            }
            invented_with_quote = {**claim, 'subject': 'invented biological entity'}
            payload = {'claims': ([claim, neighbor_only, invented_with_quote] if len(calls) == 1 else [claim])}
            validate_schema(payload, options['json_schema'])
            with pytest.raises(SchemaValidationError):
                validate_schema({'claims': [{**claim, 'object_term': 'T9999'}]}, options['json_schema'])
            return payload

    reference = catalog()
    workspace = SimpleNamespace(ONTOLOGY_DIR=production.parents[1], _lock=RLock(), _jobs={},
                                _pdf_path=lambda key: pdf, Settings=Settings, OpenCodeClient=Model)
    app = FastAPI()
    app.include_router(create_router(workspace))
    prefix = '/api/ontology/single-stage'
    with TestClient(app) as client:
        started = client.post(prefix + '/paper')
        assert started.status_code == 202
        assert client.post(prefix + '/paper?comparison=true').status_code == 422
        key = started.json()['id']
        for _ in range(100):
            result = client.get(f'{prefix}/paper/{key}').json()
            if result['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.02)
        assert result['status'] == 'completed', result
        assert client.get(prefix + '/latest/paper').json()['id'] == key
    assert production.read_text(encoding='utf-8') == 'original'
    assert len(calls) == 2 and result['completed_targets'] == 2
    assert settings_seen[0].task_thinking['extraction'] is False
    assert settings_seen[0].json_regeneration_attempts == 0
    assert result['total'] == 2 and result['rejection_count'] == 2
    rejected_file = tmp_path / 'analysis' / 'single_stage' / 'paper' / key / 'proposal_rejections.jsonl'
    assert 'context-only finding' in rejected_file.read_text(encoding='utf-8')
    claim = result['claims'][0]
    assert claim['subject'] == 'root' and claim['bindings']['object']['mapping'] == 'exact'
    assert claim['bindings']['relation']['iri'].endswith('/BFO_0000050')
    assert resolve_relation('did not significantly affect', reference)['mapping'] == 'unresolved'
    db, _ = open_lookup(index)
    with closing(db):
        assert resolve_endpoint(db, reference, 'root', 'chemical', ['root'], [])[0]['mapping'] == 'unresolved'
        assert resolve_endpoint(db, reference, 'H2PO4-', 'chemical', ['H2PO4-'], [])[0]['mapping'] == 'exact'
        assert resolve_endpoint(db, reference, 'H2PO4+', 'chemical', ['H2PO4+'], [])[0]['mapping'] == 'unresolved'
        assert resolve_endpoint(db, reference, 'root length', 'trait or measurement', [], [])[0]['iri'].endswith('TO_0000227')
        assert resolve_endpoint(db, reference, 'root length', 'assay variable', [], [])[0]['iri'].endswith('CO_330:0000762')


def test_full_lookup_ranks_the_phrase_and_preserves_unresolved_relations(tmp_path):
    rdf = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
        xmlns:obo="http://purl.obolibrary.org/obo/" xmlns:oboInOwl="http://www.geneontology.org/formats/oboInOwl#">
        <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0001166">
          <rdfs:label>starch quality trait</rdfs:label>
          <obo:IAO_0000115>Physical, biochemical and thermal properties of a starch.</obo:IAO_0000115>
          <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/TO_0000387"/>
        </owl:Class>
        <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_28017"><rdfs:label>starch</rdfs:label></owl:Class>
        <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_28659">
          <rdfs:label>phosphorus atom</rdfs:label>
          <oboInOwl:hasExactSynonym>phosphorus</oboInOwl:hasExactSynonym>
        </owl:Class>
        <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000078">
          <rdfs:label>root dry weight</rdfs:label>
          <obo:IAO_0000115>The average dry weight of the root determined in a study.</obo:IAO_0000115>
        </owl:Class>
        <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0009999">
          <rdfs:label>starch quality</rdfs:label><owl:deprecated>true</owl:deprecated>
        </owl:Class>
    </rdf:RDF>"""
    data = rdf.encode()
    (tmp_path / 'to.owl').write_bytes(data)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'to': {'file': 'to.owl', 'sha256': sha256(data).hexdigest()},
    }), encoding='utf-8')
    reference = catalog()
    db, _ = open_lookup(tmp_path)
    visible = {1: 'The quality of both tubers and starch was affected by P.'}
    glossary = [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'test'}]
    with closing(db):
        selection = passage_candidates(db, reference, visible, {1}, glossary)
        suggestions = endpoint_candidates(db, reference, 'root dry mass', ['Root dry mass differed between treatments.'])
        assert suggestions[0]['iri'].endswith('/TO_0000078')
        assert endpoint_candidates(db, reference, 'HP', ['Plants were grown under HP.']) == []
    projected, matched, hints = selection
    assert next(iter(matched)).endswith('/TO_0001166')
    assert any(iri.endswith('/CHEBI_28659') for iri in matched)
    assert all(not iri.endswith('/TO_0009999') for iri in matched)
    assert hints == {'P': 'phosphorus'}
    _, system, schema, aliases = single_stage.request_setup(reference, selection)
    assert 'Physical, biochemical and thermal properties' in system
    assert 'plant trait' in system

    def alias(role, local_id):
        return next(key for key, iri in aliases[role].items() if iri.endswith('/' + local_id))

    claim = {'subject': 'P', 'relation': 'affected', 'object': 'industrial starch performance',
             'subject_term': alias('subject', 'CHEBI_28659'), 'relation_term': 'unresolved',
             'object_term': alias('object', 'TO_0001166'), 'broader': [], 'qualifiers': {},
             'evidence_sentence_ids': [1]}
    target = {'target_id': 'example', 'target_sentence_numbers': [1], 'sentences': [{'page': 1}]}
    validate_schema({'claims': [claim]}, schema)
    # A source phrase derived by an unsupported reorder fails even with a valid ontology alias.
    diagnostics = {}
    assert single_stage.grounded_claim(claim, target, visible, projected, aliases, diagnostics) is None
    assert diagnostics['reason'] == 'unanchored_endpoint'
    visible[1] = 'P affected starch quality.'
    row = single_stage.grounded_claim({**claim, 'object': 'starch quality'}, target, visible, projected, aliases, {})
    assert row['subject'] == 'P'
    assert row['bindings']['object']['iri'].endswith('/TO_0001166')
    assert row['bindings']['relation']['mapping'] == 'unresolved' and not row['classified']

    requests = []

    class Model:
        def chat_json(self, system, passage, **options):
            requests.append(passage)
            enums = options['json_schema']['properties']['claims']['items']['properties']
            assert 'unresolved' in enums['relation_term']['enum']
            return {'claims': [{**claim, 'object': 'starch quality', 'subject_term': 'unresolved',
                                'object_term': 'unresolved'}]}

    output = tmp_path / 'run'
    single_stage.run_single_stage(
        None, reference, Model(), output, lambda **_: None, 1000, lookup_dir=tmp_path,
        prepared_targets=[{**target, 'sentences': [{'text': visible[1], 'page': 1}],
                           'source_abbreviation_definitions': glossary}],
    )
    result = json.loads((output / 'endpoint_candidates.jsonl').read_text(encoding='utf-8'))
    saved = json.loads((output / 'claims.jsonl').read_text(encoding='utf-8'))
    assert len(requests) == 1 and result['claim_id'] == saved['id']
    assert result['endpoints']['object'][0]['iri'].endswith('/TO_0001166')
    assert saved['bindings']['object']['mapping'] == 'unresolved'


def test_relation_evidence_checks_distinguish_group_difference_from_association():
    reference = catalog()
    association = {'iri': 'http://purl.obolibrary.org/obo/RO_0002610'}
    evidence = single_stage.relation_evidence_issue
    assert evidence('were significantly different between', association, reference) == 'group_difference_as_correlation'
    assert evidence('was significant for', association, reference) == 'group_difference_as_correlation'
    assert evidence('did not change ... significantly', association, reference) == 'non_significant_result_as_causal_or_correlated'
    assert evidence('was significantly correlated with', association, reference) is None
    assert evidence('was significantly different between', {'iri': None}, reference) is None



def _v3_claim(sid, subj, stype, rel, obj, otype, s_quote=None, r_quote=None, o_quote=None,
              status='affirmed', qualifiers=None, support_ids=None, s_sid=None, o_sid=None):
    return {
        'subject': subj,
        'subject_type': stype,
        'relation': rel,
        'object': obj,
        'object_type': otype,
        'assertion_status': status,
        'qualifiers': {
            'crop_or_species': None, 'population': None, 'condition': None,
            'comparator': None, 'metric': None, **(qualifiers or {}),
        },
        'support_sentence_ids': support_ids or [],
        'subject_anchor': {'sentence_id': s_sid or sid, 'quote': s_quote or subj},
        'relation_anchor': {'sentence_id': sid, 'quote': r_quote or rel},
        'object_anchor': {'sentence_id': o_sid or sid, 'quote': o_quote or obj},
    }


def _rich_index(tmp_path):
    owl = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#" xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:oboInOwl="http://www.geneontology.org/formats/oboInOwl#">
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000015"><rdfs:label>process</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000003"><rdfs:label>occurrent</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000002"><rdfs:label>continuant</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/BFO_0000020">
           <rdfs:label>specifically dependent continuant</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000002"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/NCBITaxon_4113">
           <rdfs:label>Solanum tuberosum</rdfs:label>
           <oboInOwl:hasExactSynonym>potato</oboInOwl:hasExactSynonym>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0009005"><rdfs:label>root</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PO_0025025"><rdfs:label>root system</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000227">
           <rdfs:label>root length</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000207">
           <rdfs:label>plant height</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000462">
           <rdfs:label>shoot dry weight</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000020"/>
         </owl:Class>
         <owl:Class rdf:about="http://example.org/CUSTOM_0001">
           <rdfs:label>root architectural elongation</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/TO_0000227"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0000511"><rdfs:label>phosphorus uptake</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/TO_0001166"><rdfs:label>starch quality trait</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PATO_0001029"><rdfs:label>efficiency</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_41510"><rdfs:label>H2PO4-</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/CHEBI_28659">
           <rdfs:label>phosphorus atom</rdfs:label>
           <oboInOwl:hasExactSynonym>phosphorus</oboInOwl:hasExactSynonym>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/PECO_0007397">
           <rdfs:label>phosphorus nutrient exposure</rdfs:label>
           <oboInOwl:hasNarrowSynonym>phosphorus nutrient treatment narrow</oboInOwl:hasNarrowSynonym>
           <oboInOwl:hasNarrowSynonym>phosphorus shortage</oboInOwl:hasNarrowSynonym>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/GO_0040007">
           <rdfs:label>growth</rdfs:label>
           <rdfs:subClassOf rdf:resource="http://purl.obolibrary.org/obo/BFO_0000015"/>
         </owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/ENVO_00001"><rdfs:label>amber medium</rdfs:label></owl:Class>
         <owl:Class rdf:about="http://purl.obolibrary.org/obo/ENVO_00002"><rdfs:label>amber medium</rdfs:label></owl:Class>
         </rdf:RDF>"""
    (tmp_path / 'sample.owl').write_bytes(owl)
    (tmp_path / 'inventory.json').write_text(json.dumps({
        'sample': {'file': 'sample.owl', 'sha256': sha256(owl).hexdigest()},
    }), encoding='utf-8')
    return open_lookup(tmp_path)


def test_v3_prompt_and_schema_stay_synchronized():
    assert EXTRACT_THEN_GROUND_POLICY == 'typed-extraction-local-ontology-v3'
    schema = load_extraction_schema()
    prompt, returned_schema = extraction_request(catalog())
    assert schema == returned_schema
    item = schema['properties']['claims']['items']
    assert 'assertion_status' in item['required']
    assert 'evidence_sentence_ids' not in item['properties']
    assert 'support_sentence_ids' in item['properties'] and 'support_sentence_ids' not in item['required']
    assert set(item['properties']['assertion_status']['enum']) == {'affirmed', 'negated', 'uncertain', 'non_significant'}
    for role in ('subject_anchor', 'relation_anchor', 'object_anchor'):
        assert role in item['required']
    assert 'Ontology identifiers are resolved later.' in prompt
    assert 'support_sentence_ids' in prompt and 'Example 1' in prompt and 'Example 2' in prompt
    assert 'non_significant' in prompt and 'TO_0000227' not in prompt


def test_abbreviations_handle_broken_hyphens_new_verb_filter_and_transitive_glossary():
    doc = 'doc-1'
    glossary = [
        {'abbreviation': 'P', 'expansion': 'Phosphorus', 'quote': 'Phosphorus (P)', 'document': doc},
        {'abbreviation': 'HP', 'expansion': 'high phos - phorus', 'quote': 'high phos - phorus (HP)', 'document': doc},
        {'abbreviation': 'HP', 'expansion': 'high phosphorus', 'quote': 'high phosphorus (HP)', 'document': doc},
        {'abbreviation': 'LP', 'expansion': 'low P', 'quote': 'low P (LP)', 'document': doc},
        {'abbreviation': 'LP', 'expansion': 'low phosphorus', 'quote': 'low phosphorus (LP)', 'document': doc},
        {'abbreviation': 'NeW', 'expansion': 'network had a significantly decreased width',
         'quote': 'The root network had a significantly decreased width (NeW)', 'document': doc},
        {'abbreviation': 'NeW', 'expansion': 'network width',
         'quote': 'network width (NeW)', 'document': doc},
    ]
    resolved, ambiguous = resolved_aliases(glossary, doc)
    assert ambiguous == set()
    assert resolved['HP']['expansion'] == 'high phosphorus'
    assert resolved['NeW']['expansion'] == 'network width'
    assert resolved['LP']['expansion'] in {'low P', 'low phosphorus'}

    target4_claim = {'source_document_id': doc, 'source_evidence_span': 'Plants remained at LP.'}
    kept = relevant_definitions(target4_claim, glossary)
    assert {row['abbreviation'] for row in kept} >= {'LP', 'P'}
    assert all(not row['ambiguous'] for row in kept)


def test_anchor_verification_and_predicate_fidelity_checks():
    target = {
        'target_id': 't1',
        'target_sentence_numbers': [2],
        'sentences': [{'page': 1}, {'page': 2}, {'page': 2}],
        'source_abbreviation_definitions': [
            {'abbreviation': 'LP', 'expansion': 'low phosphorus', 'quote': 'low phosphorus (LP)', 'document': 'd1'},
        ],
    }
    visible = {
        1: 'Optimal P supply affects general growth of potato (Solanum tuberosum L.).',
        2: 'Treatment T increased both root length by 12% and root surface area by 8% under LP conditions.',
        3: 'Neighboring sentence reported tuber weight increased.',
    }

    # Literal + coordinated outcomes + derived evidence_sentence_ids + support_sentence_ids pass.
    coord = _v3_claim(
        2, 'Treatment T', 'condition or exposure', 'increased', 'root surface area', 'trait or measurement',
        s_quote='Treatment T', r_quote='increased', o_quote='both root length by 12% and root surface area',
        qualifiers={'metric': '8%'}, support_ids=[1],
    )
    claim, anchors, _, ids = verify_anchors_and_evidence(coord, target, visible)
    assert claim['object'] == 'root surface area'
    assert anchors['object']['match_kind'] in {'literal_anchor', 'bounded_coordination'}
    assert ids == [1, 2]

    # Balanced closing parenthesis is preserved in resolved_span.
    paren = _v3_claim(
        2, 'Treatment T', 'condition or exposure', 'increased', 'root length', 'trait or measurement',
        s_quote='Treatment T', r_quote='increased', o_quote='root length',
        s_sid=1,
    )
    paren['subject'] = 'potato (Solanum tuberosum L.)'
    paren['subject_anchor'] = {'sentence_id': 1, 'quote': 'potato (Solanum tuberosum L.)'}
    _, paren_anchors, _, paren_ids = verify_anchors_and_evidence(paren, target, visible)
    assert paren_anchors['subject']['resolved_span'] == 'potato (Solanum tuberosum L.)'
    assert paren_ids == [1, 2]

    # Abbreviation variant resolves via glossary passed into endpoint_anchor.
    abbr = _v3_claim(
        2, 'low phosphorus', 'condition or exposure', 'reduced', 'root length', 'trait or measurement',
        s_quote='LP', r_quote='increased', o_quote='root length',
    )
    _, abbr_anchors, _, _ = verify_anchors_and_evidence(abbr, target, visible)
    assert abbr_anchors['subject']['match_kind'] == 'document_defined_abbreviation'

    # Invented endpoint paired with genuine quote fails.
    invented = _v3_claim(2, 'invented trait', 'trait or measurement', 'increased', 'root length', 'trait or measurement',
                         s_quote='root length', r_quote='increased', o_quote='root length')
    with pytest.raises(ValueError, match='Unanchored subject'):
        verify_anchors_and_evidence(invented, target, visible)

    # Neighbor-only finding cannot pass even when support_sentence_ids includes target ID.
    neighbor = _v3_claim(3, 'tuber weight', 'trait or measurement', 'increased', 'tuber mass', 'trait or measurement',
                         o_quote='tuber weight', support_ids=[2])
    with pytest.raises(SchemaValidationError, match='relation_anchor'):
        verify_anchors_and_evidence(neighbor, target, visible)

    # Predicate fidelity warnings catch strength upgrades, status mismatch, polarity contradiction, role/comparative swaps, and bundling.
    w1 = predicate_evidence_warnings(
        {'relation': 'improves', 'subject': 'optimal P supply', 'object': 'general growth'},
        'affirmed', 'affects', 'An optimal P supply not only affects general growth.',
    )
    assert 'predicate_strength_upgrade' in {w['code'] for w in w1}

    w2 = predicate_evidence_warnings(
        {'relation': 'did not significantly change', 'subject': 'P', 'object': 'ratio'},
        'affirmed', 'did not significantly change', 'P did not significantly change ratio.',
    )
    assert 'assertion_status_mismatch' in {w['code'] for w in w2}

    w3 = predicate_evidence_warnings(
        {'relation': 'improves', 'subject': 'P', 'object': 'quality of both tubers and starch'},
        'affirmed', 'improves', 'P improves quality of both tubers and starch.',
    )
    assert 'possible_bundled_outcomes' in {w['code'] for w in w3}

    w4 = predicate_evidence_warnings(
        {'relation': 'had a positive effect on', 'subject': 'P', 'object': 'growth'},
        'affirmed', 'had an overall negative effect on', 'P had an overall negative effect on growth.',
    )
    assert 'predicate_polarity_contradiction' in {w['code'] for w in w4}


def test_v3_entity_and_relation_grounding_and_clause_guards(tmp_path):
    reference = catalog()
    db, _ = _rich_index(tmp_path)
    glossary = [{'abbreviation': 'P', 'expansion': 'Phosphorus', 'quote': 'Phosphorus (P)', 'document': 'd1'}]
    with closing(db):
        # Taxonomic author wrapper vs cultivar guard
        org = resolve_endpoint(db, reference, 'potato (Solanum tuberosum L.)', 'organism',
                               ['potato (Solanum tuberosum L.)'], glossary)[0]
        assert org['mapping'] == 'exact' and org['iri'].endswith('NCBITaxon_4113')
        assert org['provenance']['class_source'] == 'sample.owl'

        cultivar = resolve_endpoint(db, reference, "potato cultivar 'Kuba'", 'organism',
                                    ["potato cultivar 'Kuba'"], glossary)[0]
        assert cultivar['mapping'] == 'unresolved'

        # Generic element P withheld from atomic phosphorus; explicit atom allowed
        gen_p = resolve_endpoint(db, reference, 'P', 'chemical', ['P has low mobility in the soil.'], glossary)[0]
        assert gen_p['mapping'] == 'unresolved' and 'atom' in gen_p['reason'].casefold()

        atom_p = resolve_endpoint(db, reference, 'phosphorus atom', 'chemical', ['phosphorus atom concentration'], glossary)[0]
        assert atom_p['mapping'] == 'exact' and atom_p['iri'].endswith('CHEBI_28659')

        # Chemical case/charge & trait suffix equivalence
        assert resolve_endpoint(db, reference, 'H2PO4-', 'chemical', ['H2PO4- uptake'], [])[0]['mapping'] == 'exact'
        assert resolve_endpoint(db, reference, 'H2PO4+', 'chemical', ['H2PO4+'], [])[0]['mapping'] == 'unresolved'
        starch = resolve_endpoint(db, reference, 'starch quality', 'trait or measurement', ['starch quality'], [])[0]
        assert starch['mapping'] == 'exact' and starch['iri'].endswith('TO_0001166')

        # Ambiguous label stays unresolved
        amb = resolve_endpoint(db, reference, 'amber medium', 'condition or exposure', ['amber medium'], [])[0]
        assert amb['mapping'] == 'unresolved' and 'Several' in amb['reason']

        # Broader via OWL-verified hasNarrowSynonym (both plain & "<term> narrow") with provenance
        narrow1 = resolve_endpoint(db, reference, 'phosphorus shortage', 'condition or exposure', ['phosphorus shortage'], [])[0]
        assert narrow1['mapping'] == 'broader' and narrow1['iri'].endswith('PECO_0007397')
        assert narrow1['provenance']['verified_narrow_synonym'] == 'phosphorus shortage'
        assert narrow1['provenance']['synonym_source'] == 'sample.owl'

        narrow2 = resolve_endpoint(db, reference, 'phosphorus nutrient treatment', 'condition or exposure',
                                   ['phosphorus nutrient treatment'], [])[0]
        assert narrow2['mapping'] == 'broader' and narrow2['iri'].endswith('PECO_0007397')
        assert verify_narrow_synonym_in_owl(tmp_path, 'http://purl.obolibrary.org/obo/PECO_0007397',
                                            'sample.owl', ['nonexistent synonym']) is None

        # Broader via unique external concept subsumed by permitted superclass
        sup = resolve_endpoint(db, reference, 'root architectural elongation', 'trait or measurement',
                               ['root architectural elongation'], [])[0]
        assert sup['mapping'] == 'broader' and sup['iri'].endswith('TO_0000227')

        # Contextual efficiency stays unresolved with efficiency candidate & class_source retrieved
        eff, eff_cands = resolve_endpoint(db, reference, 'P acquisition efficiency', 'trait or measurement',
                                          ['P acquisition efficiency'], glossary)
        assert eff['mapping'] == 'unresolved'
        pato = next(c for c in eff_cands if c['iri'].endswith('PATO_0001029'))
        assert pato['class_source'] == 'sample.owl'

        # Positive relation controls
        assert resolve_relation('is part of', reference)['iri'].endswith('BFO_0000050')
        assert resolve_relation('are parts of', reference)['iri'].endswith('BFO_0000050')
        corr = resolve_relation('was significantly correlated with', reference,
                                relation_quote='was significantly correlated with')
        assert corr['iri'].endswith('RO_0002610') and corr['mapping'] == 'broader'

        # Source-clause controls: unsupported correlation & RO_0002304 necessity across all paths
        assert resolve_relation('correlated with', reference, relation_quote='measured alongside')['mapping'] == 'unresolved'
        pos_eff = resolve_relation('had a positive effect on', reference, relation_quote='had a positive effect on')
        assert pos_eff['mapping'] == 'unresolved'
        pos_exact = resolve_relation('causally upstream of, positive effect', reference,
                                     relation_quote='increased growth rate without necessity')
        assert pos_exact['mapping'] == 'unresolved'
        assert 'required' in pos_exact['reason'] and 'frequency' in pos_exact['reason']

        # Active verbal predication vs bare nominalization / reversed clause
        assert resolve_relation('transports', reference, relation_quote='transport of assimilates',
                                local_clause='transport of assimilates was measured')['mapping'] == 'unresolved'
        assert resolve_relation('transports', reference, relation_quote='transports',
                                local_clause='root transports H2PO4-', subject_span='root', object_span='H2PO4-')['iri'].endswith('RO_0002020')

        # Negative relation controls
        assert resolve_relation('affects', reference)['mapping'] == 'unresolved'
        assert resolve_relation('causally related to', reference)['mapping'] == 'unresolved'
        assert resolve_relation('is part of', reference, assertion_status='non_significant')['mapping'] == 'unresolved'
        assert resolve_relation('were significantly different between', reference)['mapping'] == 'unresolved'

        # Reproduction 1: Polarity contradiction (negative quote + positive predicate) stays unresolved and held
        t = {'target_id': 't1', 'target_sentence_numbers': [1], 'sentences': [{'page': 1}], 'source_abbreviation_definitions': ()}
        v_contra = {1: 'phosphorus nutrient treatment had an overall negative effect on growth.'}
        p_contra = _v3_claim(1, 'phosphorus nutrient treatment', 'condition or exposure',
                             'had an overall positive effect on', 'growth', 'biological process',
                             r_quote='had an overall negative effect on')
        row_contra, _ = ground_extracted(p_contra, t, v_contra, reference, db)
        assert row_contra['bindings']['relation']['mapping'] == 'unresolved'
        assert not row_contra['classified']
        assert 'predicate_polarity_contradiction' in {h['code'] for h in row_contra['hold_reasons']}

        # Valid matching negative effect (occurrent -> occurrent) still grounds via RO_0002305
        v_ok = {1: 'phosphorus nutrient treatment had an overall negative effect on growth.'}
        p_ok = _v3_claim(1, 'phosphorus nutrient treatment', 'condition or exposure',
                         'had an overall negative effect on', 'growth', 'biological process')
        row_ok, _ = ground_extracted(p_ok, t, v_ok, reference, db)
        assert row_ok['bindings']['relation']['iri'].endswith('RO_0002305')
        assert row_ok['bindings']['relation']['mapping'] == 'broader'

        # PECO -> TO still downgrades to unresolved due to BFO continuant/occurrent incompatibility
        v_bad = {1: 'phosphorus nutrient treatment had an overall negative effect on root length.'}
        p_bad = _v3_claim(1, 'phosphorus nutrient treatment', 'condition or exposure',
                          'had an overall negative effect on', 'root length', 'trait or measurement')
        row_bad, _ = ground_extracted(p_bad, t, v_bad, reference, db)
        assert row_bad['bindings']['relation']['mapping'] == 'unresolved'
        assert 'compatible' in row_bad['bindings']['relation']['reason'].casefold()

        # Reproduction 2: Saved role reversal + 'cannot' negation triggers both holds while preserving original triple
        v_rev = {1: 'Due to its low mobility in the soil, plants cannot acquire P from deep soil layers.'}
        p_rev = _v3_claim(1, 'P', 'chemical',
                          'cannot acquire from deep soil layers due to its low mobility in the soil', 'plants', 'organism',
                          r_quote='plants cannot acquire P from deep soil layers')
        row_rev, _ = ground_extracted(p_rev, t, v_rev, reference, db)
        rev_codes = {h['code'] for h in row_rev['hold_reasons']}
        assert {'assertion_status_mismatch', 'possible_agent_role_reversal'} <= rev_codes
        assert row_rev['subject'] == 'P' and row_rev['object'] == 'plants' and not row_rev['classified']

        # Passive voice must NOT falsely trigger active role reversal (including intervening adverbs and 'with')
        v_pass = {1: 'growth was reduced by phosphorus nutrient treatment.'}
        p_pass = _v3_claim(1, 'phosphorus nutrient treatment', 'condition or exposure',
                           'reduced', 'growth', 'biological process', r_quote='was reduced by')
        row_pass, _ = ground_extracted(p_pass, t, v_pass, reference, db)
        assert 'possible_agent_role_reversal' not in {h['code'] for h in row_pass['hold_reasons']}

        v_pass_adv = {1: 'Both total root length and root surface area were significantly impacted by P levels, cultivar, and their interaction.'}
        p_pass_adv = _v3_claim(
            1, 'P levels, cultivar, and their interaction', 'condition or exposure',
            'impacted', 'total root length', 'trait or measurement', r_quote='were significantly impacted by',
        )
        row_pass_adv, _ = ground_extracted(p_pass_adv, t, v_pass_adv, reference, db)
        assert 'possible_agent_role_reversal' not in {h['code'] for h in row_pass_adv['hold_reasons']}

        v_pass_with = {1: 'Leaf areas were enhanced with microbial inoculation.'}
        p_pass_with = _v3_claim(
            1, 'microbial inoculation', 'condition or exposure',
            'enhanced', 'Leaf areas', 'trait or measurement', r_quote='were enhanced with',
        )
        row_pass_with, _ = ground_extracted(p_pass_with, t, v_pass_with, reference, db)
        assert 'possible_agent_role_reversal' not in {h['code'] for h in row_pass_with['hold_reasons']}

        # Reproduction 3: Comparative trait-vs-group swap flagged while preserving claim
        v_comp = {1: 'phosphorus uptake of the small rooting type was 19.5% less than that of intermediate ones.'}
        p_comp = _v3_claim(1, 'small rooting type', 'trait or measurement',
                           'was 19.5% less than', 'phosphorus uptake', 'trait or measurement',
                           r_quote='was 19.5% less than')
        row_comp, _ = ground_extracted(p_comp, t, v_comp, reference, db)
        assert 'possible_comparative_role_swap' in {h['code'] for h in row_comp['hold_reasons']}

        # Reproduction 4: Case 3 clause-scoped negation avoids false possible_lost_negation on valid decrease
        v_c3 = {1: 'The root network had a significantly decreased width, showing that plants did not react.'}
        p_c3 = _v3_claim(1, 'The root network', 'plant part', 'had a significantly decreased', 'width', 'trait or measurement')
        row_c3, _ = ground_extracted(p_c3, t, v_c3, reference, db)
        assert 'possible_lost_negation' not in {h['code'] for h in row_c3['hold_reasons']}
        assert row_c3['review'] == 'not_flagged'

        # Reproduction 5: Cross-clause relation cue cannot bind across separate coordinated predicates
        v_cross = {1: 'Root length correlated with plant height and was measured alongside shoot dry weight.'}
        p_cross = _v3_claim(
            1, 'Root length', 'trait or measurement', 'correlated with', 'shoot dry weight', 'trait or measurement',
            s_quote='Root length', r_quote='correlated with', o_quote='shoot dry weight',
        )
        row_cross, _ = ground_extracted(p_cross, t, v_cross, reference, db)
        assert row_cross['bindings']['relation']['mapping'] == 'unresolved'
        assert row_cross['bindings']['subject']['iri'].endswith('TO_0000227')
        assert row_cross['bindings']['object']['iri'].endswith('TO_0000462')
        assert 'cross_clause_relation_mismatch' in {h['code'] for h in row_cross['hold_reasons']}
        assert row_cross['review'] == 'held' and not row_cross['classified']

        # Same sentence with the actual same-clause object passes completely
        p_same = _v3_claim(
            1, 'Root length', 'trait or measurement', 'correlated with', 'plant height', 'trait or measurement',
            s_quote='Root length', r_quote='correlated with', o_quote='plant height',
        )
        row_same, _ = ground_extracted(p_same, t, v_cross, reference, db)
        assert row_same['bindings']['relation']['iri'].endswith('RO_0002610')
        assert 'cross_clause_relation_mismatch' not in {h['code'] for h in row_same['hold_reasons']}
        assert row_same['classified']

        # Resolver-level necessity cue from an unrelated clause stays unresolved
        rel_cross_nec = resolve_relation(
            'causally upstream of, positive effect',
            reference,
            relation_quote='is required for',
            local_clause='Phosphorus is required for metabolism and positively affected growth.',
            subject_span='Phosphorus',
            object_span='growth',
        )
        assert rel_cross_nec['mapping'] == 'unresolved'

        # Coordinated noun objects sharing one verb do NOT falsely trigger cross-clause boundary
        assert _spans_separated_by_clause_boundary(
            'Treatment T increased both root length by 12% and root surface area by 8%.',
            'Treatment T',
            'increased',
            'root surface area',
        ) is None



def test_single_stage_route_reads_saved_v1_baseline_runs(tmp_path):
    production = tmp_path / 'ontology' / 'paper' / 'candidates.jsonl'
    production.parent.mkdir(parents=True)
    run_id = '3c57b14952084017bf7213596e8d4c9e'
    run_dir = tmp_path / 'analysis' / 'single_stage' / 'paper' / run_id
    run_dir.mkdir(parents=True)
    (run_dir / 'run.json').write_text(json.dumps({
        'id': run_id, 'status': 'completed', 'workflow': 'typed-extraction-local-ontology-v1',
        'sample': True, 'total_targets': 1, 'completed_targets': 1, 'claim_count': 1, 'classified_count': 0,
    }), encoding='utf-8')
    (run_dir / 'sample.json').write_text(json.dumps([
        {'id': 't1', 'title': 'Abstract findings', 'review': 'Review', 'page': 1, 'source': 'Plants need P.'},
    ]), encoding='utf-8')
    (run_dir / 'claims.jsonl').write_text(json.dumps({
        'id': 'c1', 'target_id': 't1', 'subject': 'plants', 'relation': 'need', 'object': 'P',
        'bindings': {
            'subject': {'iri': 'http://purl.obolibrary.org/obo/NCBITaxon_33090', 'label': 'Viridiplantae', 'mapping': 'exact', 'reason': 'ok'},
            'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'no match'},
            'object': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'generic P'},
        },
        'qualifiers': {}, 'review': 'not_flagged', 'source_evidence_span': 'Plants need P.', 'source_page': 1,
    }) + '\n', encoding='utf-8')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'fixture')
    workspace = SimpleNamespace(ONTOLOGY_DIR=production.parents[1], _lock=RLock(), _jobs={},
                                _pdf_path=lambda key: pdf, Settings=Settings, OpenCodeClient=None)
    app = FastAPI()
    app.include_router(create_router(workspace))
    with TestClient(app) as client:
        resp = client.get(f'/api/ontology/single-stage/paper/{run_id}')
        assert resp.status_code == 200
        payload = resp.json()
        assert payload['workflow'] == 'typed-extraction-local-ontology-v1'
        assert payload['cases'][0]['claims'][0]['subject'] == 'plants'


def test_single_stage_route_defaults_deterministic_and_accepts_verifier_option(tmp_path):
    production = tmp_path / 'ontology' / 'paper' / 'candidates.jsonl'
    production.parent.mkdir(parents=True)
    inv = tmp_path / 'analysis' / 'ontology_full_releases' / 'inventory.json'
    inv.parent.mkdir(parents=True)
    inv.write_text('{}', encoding='utf-8')
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'')  # Empty so worker exits fast after writing queued/failed state

    workspace = SimpleNamespace(ONTOLOGY_DIR=production.parents[1], _lock=RLock(), _jobs={},
                                _pdf_path=lambda key: pdf, Settings=Settings, OpenCodeClient=None)
    app = FastAPI()
    app.include_router(create_router(workspace))
    with TestClient(app) as client:
        r_default = client.post('/api/ontology/single-stage/paper')
        assert r_default.status_code == 202
        j_default = r_default.json()
        assert j_default['verifier'] is False
        assert j_default['verifier_policy'] is None
        for t in list(workspace._single_stage_tokens.values()):
            t.request()
        workspace._single_stage_tokens.clear()

        r_ver = client.post('/api/ontology/single-stage/paper?verifier=true')
        assert r_ver.status_code == 202
        j_ver = r_ver.json()
        assert j_ver['verifier'] is True
        assert j_ver['verifier_policy'] == 'bounded-endpoint-verifier-v2'
        assert j_ver['selective_thinking'] is True
        for t in list(workspace._single_stage_tokens.values()):
            t.request()
        workspace._single_stage_tokens.clear()


def test_single_stage_route_verifier_wires_selective_thinking(tmp_path, monkeypatch):
    from backend.api.routes import single_stage as route_mod

    production = tmp_path / 'ontology' / 'paper' / 'candidates.jsonl'
    production.parent.mkdir(parents=True)
    idx_dir = tmp_path / 'analysis' / 'ontology_full_releases'
    idx_dir.mkdir(parents=True)
    db, _ = _rich_index(idx_dir)
    db.close()
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'dummy')

    targets = [
        {
            'target_id': 't_simple',
            'title': 'Simple passage',
            'review': 'r1',
            'section_heading': 'Results',
            'page': 1,
            'target_sentence_numbers': [1],
            'context_sentence_numbers': [],
            'sentences': [{'page': 1, 'text': 'Root length correlated with plant height.'}],
            'source_abbreviation_definitions': [],
            'extraction_focus': 'Extract findings.',
        },
        {
            'target_id': 't_complex',
            'title': 'Coupled percentage comparison',
            'review': 'r2',
            'section_heading': 'Results',
            'page': 2,
            'target_sentence_numbers': [1],
            'context_sentence_numbers': [],
            'sentences': [{'page': 2, 'text': 'P uptake was 19.5% and 23.7% less than intermediate and large ones, respectively.'}],
            'source_abbreviation_definitions': [],
            'extraction_focus': 'Extract findings.',
        },
    ]
    cases = [{'id': t['target_id'], 'title': t['title'], 'review': t['review'], 'page': t['page'],
              'source': t['sentences'][0]['text']} for t in targets]

    monkeypatch.setattr(route_mod, 'prepare_sample', lambda *a, **kw: list(zip(cases, targets)))
    monkeypatch.setattr(route_mod, 'source_targets', lambda *a, **kw: targets)
    monkeypatch.setattr(route_mod, 'file_hash', lambda *a, **kw: route_mod.PDF_HASH)

    recorded_calls = []

    class FakeOpenCodeClient:
        def __init__(self, settings, *, task, on_request=None, response_dir=None):
            self.thinking_enabled = bool((settings.thinking or {}).get(task, False))

        def chat_json(self, system, user_msg, **options):
            recorded_calls.append({'thinking': self.thinking_enabled, 'system': system})
            if 'Verify local ontology class candidates' in system:
                payload = json.loads(user_msg)
                return {'decisions': [
                    {'item_id': it['item_id'], 'decision': 'unresolved', 'candidate_alias': None,
                     'type_challenged': False, 'revised_type': None, 'justification': 'Abstain.'}
                    for it in payload['items']
                ]}
            if '19.5%' in user_msg:
                return {'claims': [_v3_claim(1, 'P uptake', 'trait or measurement', 'was 19.5% less than',
                                             'intermediate ones', 'organism', s_quote='P uptake',
                                             r_quote='was 19.5% less than', o_quote='intermediate')]}
            return {'claims': [_v3_claim(1, 'Root length', 'trait or measurement', 'correlated with',
                                         'plant height', 'trait or measurement', s_quote='Root length',
                                         r_quote='correlated with', o_quote='plant height')]}

    workspace = SimpleNamespace(ONTOLOGY_DIR=production.parents[1], _lock=RLock(), _jobs={},
                                _pdf_path=lambda key: pdf, Settings=Settings, OpenCodeClient=FakeOpenCodeClient)
    app = FastAPI()
    app.include_router(create_router(workspace))
    with TestClient(app) as client:
        resp_default = client.post('/api/ontology/single-stage/paper?sample=true')
        assert resp_default.status_code == 202
        rid_default = resp_default.json()['id']
        for _ in range(200):
            st_def = client.get(f'/api/ontology/single-stage/paper/{rid_default}').json()
            if st_def['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.05)
        assert st_def['status'] == 'completed', st_def
        default_calls = [c['thinking'] for c in recorded_calls]
        assert default_calls == [False, False]
        recorded_calls.clear()

        resp = client.post('/api/ontology/single-stage/paper?sample=true&verifier=true')
        assert resp.status_code == 202
        run_id = resp.json()['id']
        for _ in range(200):
            st = client.get(f'/api/ontology/single-stage/paper/{run_id}').json()
            if st['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.05)
        assert st['status'] == 'completed', st

    extraction_calls = [c['thinking'] for c in recorded_calls if 'Verify local ontology class candidates' not in c['system']]
    assert extraction_calls == [False, True]
    # The experimental route preserves its selective Thinking setup.
    assert Settings().task_thinking['extraction'] is True


def test_relation_catalogue_mapped_vs_unresolved_examples(tmp_path):
    reference = catalog()
    assert len(reference.profile['allowed_object_properties']) == 101
    db, _ = _rich_index(tmp_path)
    with closing(db):
        t = {'target_id': 't_rel', 'target_sentence_numbers': [1], 'sentences': [{'page': 1}], 'source_abbreviation_definitions': ()}

        # Mapped examples under the existing 23-property catalogue:
        # 1) BFO:0000050 (part of) at resolver level
        v_part = {1: 'root is part of shoot system.'}
        r_part = resolve_relation('is part of', reference, relation_quote='is part of', local_clause=v_part[1], subject_span='root', object_span='shoot system')
        assert r_part['iri'].endswith('BFO_0000050')

        # 2) Full ground_extracted mapped example: RO_0002610 (correlated with) on trait -> trait
        v_corr = {1: 'Root length correlated with plant height.'}
        p_corr = _v3_claim(1, 'Root length', 'trait or measurement', 'correlated with', 'plant height', 'trait or measurement')
        row_corr, _ = ground_extracted(p_corr, t, v_corr, reference, db)
        assert row_corr['bindings']['relation']['iri'].endswith('RO_0002610')
        assert row_corr['classified'] is True

        # Unresolved reviewed comparisons (vocabulary scope gap, not resolver failure):
        v_cmp = {1: 'phosphorus uptake of the small rooting type was 19.5% less than that of intermediate ones.'}
        p_cmp = _v3_claim(1, 'phosphorus uptake', 'trait or measurement', 'was 19.5% less than', 'intermediate ones', 'organism',
                          r_quote='was 19.5% less than', o_quote='intermediate ones')
        row_cmp, _ = ground_extracted(p_cmp, t, v_cmp, reference, db)
        assert row_cmp['bindings']['relation']['iri'] is None
        assert row_cmp['bindings']['relation']['mapping'] == 'unresolved'
        assert resolve_relation('affects', reference, relation_quote='affects', local_clause='P affects growth.', subject_span='P', object_span='growth')['mapping'] == 'unresolved'
        assert resolve_relation('higher in', reference, relation_quote='higher in', local_clause='P was higher in LP.', subject_span='P', object_span='LP')['mapping'] == 'unresolved'


def test_explicit_nominal_correlation_and_cultivar_contrast(tmp_path):
    reference = catalog()
    db, _ = _rich_index(tmp_path)
    contrast_sentence = (
        'Root length and root surface area had a significant correlation with phosphorus uptake in Milva, '
        'but no such relationships were found in Lady Claire.'
    )
    t = {'target_id': 't_corr', 'target_sentence_numbers': [1], 'sentences': [{'page': 1}], 'source_abbreviation_definitions': ()}
    visible = {1: contrast_sentence}
    with closing(db):
        # Clause 1 (Milva): grounds to broader RO_0002610 and preserves qualifiers
        p_milva = {
            **_v3_claim(
                1, 'Root length', 'trait or measurement', 'had a significant correlation with', 'phosphorus uptake', 'trait or measurement',
                s_quote='Root length', r_quote='had a significant correlation with', o_quote='phosphorus uptake',
            ),
            'qualifiers': {'crop_or_species': 'potato', 'population': 'Milva', 'significance': 'significant'},
        }
        row_milva, _ = ground_extracted(p_milva, t, visible, reference, db)
        assert row_milva['bindings']['relation']['iri'].endswith('RO_0002610')
        assert row_milva['bindings']['relation']['mapping'] == 'broader'
        assert row_milva['qualifiers']['population'] == 'Milva'
        assert row_milva['qualifiers']['significance'] == 'significant'
        assert row_milva['review'] == 'not_flagged'

        # Displayed a positive correlation with also maps cleanly as broader
        r_pos = resolve_relation(
            'displayed a positive correlation with', reference,
            relation_quote='displayed a positive correlation with',
            local_clause='specific P uptake displayed a positive correlation with total P uptake.',
            subject_span='specific P uptake', object_span='total P uptake',
        )
        assert r_pos['iri'].endswith('RO_0002610') and r_pos['mapping'] == 'broader'

        # Clause 2 (Lady Claire negative contrast): MUST abstain (unresolved)
        p_lady_neg = {
            **_v3_claim(
                1, 'Root length', 'trait or measurement', 'had no such relationship with', 'phosphorus uptake', 'trait or measurement',
                s_quote='Root length', r_quote='no such relationships were found', o_quote='phosphorus uptake',
                status='negated',
            ),
            'qualifiers': {'population': 'Lady Claire'},
        }
        row_lady, _ = ground_extracted(p_lady_neg, t, visible, reference, db)
        assert row_lady['bindings']['relation']['iri'] is None
        assert row_lady['bindings']['relation']['mapping'] == 'unresolved'

        # Cross-clause contamination attempt linking Lady Claire across 'but' must also abstain
        p_cross_lc = _v3_claim(
            1, 'Root length', 'trait or measurement', 'had a significant correlation with', 'Lady Claire', 'organism',
            s_quote='Root length', r_quote='had a significant correlation with', o_quote='Lady Claire',
        )
        row_cross_lc, _ = ground_extracted(p_cross_lc, t, visible, reference, db)
        assert row_cross_lc['bindings']['relation']['iri'] is None
        assert 'cross_clause_relation_mismatch' in {h['code'] for h in row_cross_lc['hold_reasons']}

        # Prompt & schema synchronize cleanly with contiguous anchor + joint-effect rules (and zero test leakage)
        prompt, _ = extraction_request(reference)
        assert 'Example 3' in prompt and 'genuine joint effects' in prompt
        assert '19.5%' not in prompt and '23.7%' not in prompt and 'Milva' not in prompt

        # Unsplit coordinated distinct traits ('specific P uptake and root efficiency') are held as possible_bundled_outcomes
        p_bundled_subj = _v3_claim(
            1, 'specific P uptake and root efficiency', 'trait or measurement',
            'displayed a positive correlation with', 'total P uptake', 'trait or measurement',
            s_quote='specific P uptake and root efficiency',
            r_quote='displayed a positive correlation with',
            o_quote='total P uptake',
        )
        t_bund = {
            'target_id': 'tb1', 'target_sentence_numbers': [1], 'sentences': [{'page': 6}],
            'source_abbreviation_definitions': [{'abbreviation': 'P', 'expansion': 'phosphorus', 'quote': 'phosphorus (P)', 'document': 'd1'}],
        }
        row_bund, _ = ground_extracted(
            p_bundled_subj, t_bund,
            {1: 'In both cultivars, specific P uptake and root efficiency displayed a positive correlation with total P uptake.'},
            reference, db,
        )
        assert 'possible_bundled_outcomes' in {h['code'] for h in row_bund['hold_reasons']}
        assert row_bund['classified'] is False

