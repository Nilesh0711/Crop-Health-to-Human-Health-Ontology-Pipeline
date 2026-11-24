"""Offline controls for the frozen-sample repair cycle; no provider requests."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.cli.repair_sample import (
    load_sample, new_output, policies, repair_signature, RecordingClient, report_experiment,
)
from backend.domain.extraction_review import extraction_warnings, review_status, assertion_eligible
from backend.domain.classification import classification_complete
from backend.stages.claim_extraction.anchoring import endpoint_anchor, ANCHORING_POLICY
from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION, EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.ontology_grounding.contract import GROUNDING_INSTRUCTIONS
from backend.ontology.policies import GROUNDING_POLICY


def proposal(obj, source=None, **extra):
    return {'subject': 'Trait Q', 'relation': 'increased to', 'object': obj, 'qualifiers': {},
            'source_evidence_span': source or f'Trait Q increased to {obj}.', **extra}


@pytest.mark.parametrize('value,code', [
    ('30.18 mg g−1', 'measurement'), ('16.50 mg g − 1', 'measurement'),
    ('12%', 'measurement'), ('7 mg/g', 'measurement'), ('42', 'measurement'),
    ('25 to 42 days', 'temporal'),
    ('25 days after transplantation to 42 days after transplantation', 'temporal'),
])
def test_pure_numeric_or_temporal_endpoints_are_visible_holds(value, code):
    c = proposal(value)
    before = deepcopy(c)
    warnings = extraction_warnings(c)
    assert f'possible_{code}_endpoint' in {w['code'] for w in warnings}
    assert c == before  # Diagnostics must never rewrite a scientific assertion.
    row = {'extraction_warnings': warnings, 'claim_signature': 'new', 'scope': 'in_scope'}
    assert review_status(row) == 'held'
    assert not assertion_eligible(row)
    assert not classification_complete(row, None)


@pytest.mark.parametrize('value', [
    'H2PO4−', 'HPO4²−', 'phosphorus deficiency', 'root-to-shoot ratio',
    'root length', 'only one of 22 traits', 'Compound Q concentration', '1:2 ratio',
    'Combined Treatment T and Treatment U',
])
def test_defining_concepts_and_joint_treatments_are_not_quantities(value):
    assert not any('endpoint' in w['code'] for w in extraction_warnings(proposal(value)))


def test_quantities_must_be_source_bound_and_do_not_use_unrelated_numbers():
    assert not extraction_warnings(proposal('12%', 'Trait Q was measured.'))
    c = proposal('Trait R', 'Treatment T increased Trait R by 12% after 14 days.', subject='Treatment T',
                 relation='increased', qualifiers={'metric': '12%', 'condition': 'after 14 days'})
    assert not extraction_warnings(c)


def test_mechanism_warning_does_not_require_repeated_population():
    c = {'subject': 'Group A', 'relation': 'resulting in enhanced', 'object': 'Trait R',
         'qualifiers': {}, 'source_evidence_span':
         'Group A exhibited higher Trait Q and Trait S, resulting in enhanced Trait R.'}
    assert 'possible_mechanism_substitution' in {w['code'] for w in extraction_warnings(c)}
    valid = {'subject': 'Group A', 'relation': 'had higher', 'object': 'Trait Q',
             'qualifiers': {'comparator': 'Group B'},
             'source_evidence_span': 'Group A had higher Trait Q than Group B.'}
    assert not extraction_warnings(valid)
    ordinary = {'subject': 'Treatment T', 'relation': 'enhancing', 'object': 'Trait Q',
                'source_evidence_span': 'Treatment T, enhancing Trait Q, was observed.'}
    assert not extraction_warnings(ordinary)


def test_plural_coordination_and_qualifications_are_not_rewritten():
    sentence = 'Compound Q increased shoot and root contents by 8% and 12%, respectively.'
    assert endpoint_anchor('shoot contents', 'shoot and root contents', [sentence])
    assert not endpoint_anchor('shoot content', 'shoot and root contents', [sentence])
    assert not endpoint_anchor('invented trait', 'shoot and root contents', [sentence])
    assert ANCHORING_POLICY.endswith('v2')
    assert EXTRACTION_CONTRACT_VERSION == 20
    assert GROUNDING_POLICY.endswith('v17')


def test_generation_and_grounding_instructions_preserve_semantic_boundaries():
    prompt = build_extraction_prompt(0, 'target_context')
    assert 'passive endpoint order' in prompt and 'concessive' in prompt
    assert 'measurement time' in prompt and 'grammatical number' in prompt
    assert 'inherited' in prompt and 'joint mechanisms' in prompt
    assert 'experimentally manipulated' in GROUNDING_INSTRUCTIONS
    assert 'never add temporal ordering' in GROUNDING_INSTRUCTIONS
    assert 'Do not reorder endpoints' in GROUNDING_INSTRUCTIONS
    assert 'usage_notes' in GROUNDING_INSTRUCTIONS
    assert set(EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']['properties']['qualifiers']['properties']) == {
        'crop_or_species', 'population', 'condition', 'comparator', 'metric'}


def test_frozen_targets_cannot_bypass_targeting_policies(tmp_path, monkeypatch):
    from types import SimpleNamespace
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'source')
    monkeypatch.setattr('backend.stages.claim_extraction.extractor.source_paper_title', lambda p: 'Source')
    monkeypatch.setattr('backend.stages.claim_extraction.extractor.source_article_identifiers', lambda p: {})
    with pytest.raises(ValueError, match='Frozen production target'):
        extract_claims(pdf, SimpleNamespace(), 0, lambda *a: None,
            packets=[{'sentences': [], 'target_id': 'old'}], extraction_mode='target_context',
            prepared_targets=True, bibliography={}, source_identifiers={})


def test_protected_outputs_and_previous_experiments_are_not_overwritten(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        new_output(Path('ontology/illegal-experiment'))
    with pytest.raises(ValueError, match='outside'):
        new_output(Path('archive/backup/illegal-experiment'))
    with pytest.raises(ValueError, match='overwritten'):
        new_output(tmp_path)


def test_frozen_sample_mutations_are_rejected(tmp_path):
    sample = tmp_path / 'sample.json'
    sample.write_text('{}', encoding='utf-8')
    (tmp_path / 'sample.sha256.json').write_text('{"sha256":"wrong"}', encoding='utf-8')
    with pytest.raises(ValueError, match='Frozen sample changed'):
        load_sample(sample)


def test_request_capture_retains_options_without_serializing_callbacks(tmp_path):
    from types import SimpleNamespace
    calls = []
    client = SimpleNamespace(settings='settings', chat_json=lambda *a, **kw: calls.append((a, kw)) or {'claims': []})
    recorder = RecordingClient(client, tmp_path, system_override='frozen baseline')
    assert recorder.chat_json('new', 'frozen source', max_tokens=50000, response_check=lambda x: None) == {'claims': []}
    archived = json.loads((tmp_path / 'input-001.json').read_text(encoding='utf-8'))
    assert archived['system'] == 'frozen baseline' and archived['options']['max_tokens'] == 50000
    assert 'response_check' not in archived['options']
    assert calls[0][0] == ('frozen baseline', 'frozen source')


def test_report_keeps_technical_failures_separate_from_unresolved(tmp_path):
    folder = tmp_path / 'doc'
    folder.mkdir()
    (folder / 'technical_failure.json').write_text('{"error":"timeout","scientific_status":"unassessed"}')
    report = report_experiment(tmp_path)
    assert report['technical_failures'] == 1 and report['relations'] == {}
    assert report['grounded_records'] == 0 and report['requests'] == 0
    with pytest.raises(ValueError, match='Report exists'):
        report_experiment(tmp_path)


def test_review_acceptance_expires_when_claim_signature_changes():
    row = {'claim_signature': 'new', 'extraction_warnings': [{'code': 'held'}],
           'extraction_review': {'status': 'accepted', 'claim_signature': 'old'}}
    assert review_status(row) == 'held'
    row['extraction_review']['claim_signature'] = 'new'
    assert review_status(row) == 'accepted'
    row['mapping_consistency_warnings'] = [{'code': 'conflict'}]
    assert not assertion_eligible(row)
    assert policies()['review'].endswith('v12') and len(repair_signature()) == 64


def test_bare_time_unit_endpoint_warning_requires_change_interval():
    claim = {'subject': 'Trait Q', 'relation': 'increased from 25 days after transplantation to 45 days after transplantation',
             'object': 'days after transplantation', 'object_source_mention': 'DAT', 'qualifiers': {},
             'source_evidence_span': 'Trait Q increased from 25 DAT to 45 DAT.'}
    assert 'possible_temporal_endpoint' in {w['code'] for w in extraction_warnings(claim)}
    non_change = {'subject': 'Stage 1', 'relation': 'denotes', 'object': 'days after transplantation',
                  'qualifiers': {}, 'source_evidence_span': 'Stage 1 denotes days after transplantation.'}
    assert not extraction_warnings(non_change)


def test_self_effect_warning_is_narrow_and_preserves_anatomical_distinctions():
    wrong = {'subject': 'Trait Q in Organ O of Group A', 'relation': 'lessened', 'object': 'Trait Q in Organ O',
             'qualifiers': {}, 'source_evidence_span': 'Trait Q in Organ O of Group A lessened at Stage 2.'}
    assert 'possible_self_referential_endpoint' in {w['code'] for w in extraction_warnings(wrong)}
    distinct_organs = {'subject': 'Trait Q in roots', 'relation': 'lessened', 'object': 'Trait Q in leaves',
                       'qualifiers': {}, 'source_evidence_span': 'Trait Q in roots lessened while Trait Q in leaves rose.'}
    assert 'possible_self_referential_endpoint' not in {w['code'] for w in extraction_warnings(distinct_organs)}
    comparative = {'subject': 'Trait Q in Organ O of Group A', 'relation': 'was lower than', 'object': 'Trait Q in Organ O',
                   'qualifiers': {'comparator': 'Group B'},
                   'source_evidence_span': 'Trait Q in Organ O of Group A was lower than Group B.'}
    assert 'possible_self_referential_endpoint' not in {w['code'] for w in extraction_warnings(comparative)}


def test_parent_properties_and_subproperty_provenance_are_projected(tmp_path):
    import hashlib
    from rdflib import Graph, URIRef, Literal
    from rdflib.namespace import RDF, RDFS, OWL
    from backend.ontology.catalog import Catalog
    from backend.stages.ontology_grounding.grounder import catalog_term_map

    base = 'http://purl.obolibrary.org/obo/'
    g = Graph()
    g.add((URIRef(base + 'BFO_0000040'), RDF.type, OWL.Class))
    g.add((URIRef(base + 'BFO_0000040'), RDFS.label, Literal('material entity')))
    g.add((URIRef(base + 'BFO_0000015'), RDF.type, OWL.Class))
    g.add((URIRef(base + 'BFO_0000015'), RDFS.label, Literal('process')))
    p_child, p_mid, p_top = URIRef(base + 'RO_0002596'), URIRef(base + 'RO_0002500'), URIRef(base + 'RO_0002595')
    for prop, label in ((p_child, 'capable of regulating'), (p_mid, 'causal agent in process'),
                        (p_top, 'causal relation between material entity and a process')):
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.label, Literal(label)))
    g.add((p_child, RDFS.subPropertyOf, p_mid))
    g.add((p_mid, RDFS.subPropertyOf, p_top))
    g.add((p_top, RDFS.domain, URIRef(base + 'BFO_0000040')))
    g.add((p_top, RDFS.range, URIRef(base + 'BFO_0000015')))
    g.add((p_top, URIRef(base + 'IAO_0000232'), Literal('Do not use this relation directly.')))
    raw = g.serialize(format='turtle').encode('utf-8')
    (tmp_path / 'mod.ttl').write_bytes(raw)
    (tmp_path / 'profile.json').write_text(json.dumps({
        'allowed_classes': [base + 'BFO_0000040', base + 'BFO_0000015'],
        'allowed_object_properties': [str(p_child)],
    }))
    (tmp_path / 'manifest.json').write_text(json.dumps({'modules': [{'file': 'mod.ttl', 'format': 'turtle',
        'sha256': hashlib.sha256(raw).hexdigest(), 'ontology': 'RO', 'term_iris': [str(p_child), str(p_mid), str(p_top), base + 'BFO_0000040', base + 'BFO_0000015'],
        'source': 'https://example.test/ro'}]}))
    ref = Catalog(tmp_path)
    _, _, _, relations, _ = catalog_term_map(ref)
    r1 = relations['R1']
    assert [p['label'] for p in r1['parent_properties']] == [
        'causal agent in process', 'causal relation between material entity and a process']
    assert r1['parent_properties'][1]['domain'] == ['material entity']
    assert r1['parent_properties'][1]['range'] == ['process']
    assert r1['parent_properties'][1]['usage_notes'] == ['Do not use this relation directly.']
    assert len(r1['subproperty_provenance']) == 2


def test_adapter_uses_production_extraction_and_grounding_without_paid_transport(tmp_path, monkeypatch):
    from dataclasses import asdict
    from pypdf import PdfWriter
    from backend.config.settings import Settings
    from backend.ontology.catalog import catalog
    from backend.stages.claim_extraction.targeting import build_extraction_targets, targeted_prompt
    from backend.shared.storage import file_hash
    from backend.cli import repair_sample as adapter
    from tests.backend.test_ontology_grounding import Grounder
    root = tmp_path / 'workspace'
    (root / 'pdf').mkdir(parents=True)
    pdf = root / 'pdf/doc.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(pdf)
    packet = {'packet_number': 1, 'section_heading': 'Result', 'page_start': 1, 'page_end': 1,
              'word_count': 7, 'estimated_token_count': 20, 'sentences': [
                  {'text': 'A root is part of a root system.', 'page': 1}]}
    target = build_extraction_targets([packet], 200)[0]
    reference = catalog()
    settings = Settings()
    sample = {'catalog_signature': reference.signature, 'policies': policies(), 'settings': asdict(settings),
        'baseline_extraction_schema': EXTRACTION_RESPONSE_SCHEMA, 'baseline_grounding_system': 'baseline grounding',
        'documents': {'doc': {'pdf_sha256': file_hash(pdf), 'targets': [target],
            'scope_instructions': reference.extraction_instructions(),
            'baseline_claims': [], 'extraction_requests': [{'system': 'baseline extraction',
                'user': targeted_prompt(target, 'doc')}]}},
        'cases': [{'case_id': 'one', 'document': 'doc', 'partition': 'development',
                   'target_ids': [target['target_id']], 'baseline_mappings': [], 'expectations': 'pending',
                   'baseline_policy_compatible': True}]}
    sample_file = tmp_path / 'sample.json'
    sample_file.write_text('{}')
    calls = []
    class OfflineClient(Grounder):
        def __init__(self, supplied_settings, *, task, **kw):
            super().__init__()
            self.settings, self.task = supplied_settings, task
            self.model = settings.task_models[task]
            self.metrics = kw['on_request']

        def chat_json(self, system, user, **kw):
            calls.append((self.task, system, self.settings))
            if self.task == 'extraction':
                value = {'claims': [{'subject': 'root', 'relation': 'part of', 'object': 'root system',
                                    'qualifiers': {}, 'evidence_sentence_ids': [1]}]}
                kw['response_check'](value)
            else:
                value = super().chat_json(system, user, **kw)
            self.metrics({'status': 'ok', 'task': self.task, 'elapsed_seconds': 0.01})
            return value
    monkeypatch.setattr(adapter, 'PROJECT_ROOT', root)
    monkeypatch.setattr('backend.providers.llm.OpenCodeClient', OfflineClient)
    output = tmp_path / 'experiment'
    result = adapter.run_model(sample, sample_file, output, operation='combined', variant='repaired')
    assert result['technical_failures'] == 0 and result['claims'] == result['grounded_records'] == 1
    assert [c[0] for c in calls] == ['extraction', 'pathway_assembly']
    assert all(c[2].json_repair_attempts == c[2].json_regeneration_attempts == 0 for c in calls)
    assert calls[0][2].thinking['extraction'] is False and calls[1][2].thinking['pathway_assembly'] is True
    assert (output / 'doc/input-001.json').exists()
    assert (output / 'doc/grounding/input-001.json').exists()
    assert result['cases'][0]['meaning_review'] == 'pending'
    with pytest.raises(ValueError, match='Reserved comparison requires'):
        adapter.run_model(sample, sample_file, tmp_path / 'reserved', operation='extraction',
                          variant='repaired', reserved=True)
    with pytest.raises(ValueError, match='other partition'):
        adapter.run_model(sample, sample_file, tmp_path / 'unknown', operation='extraction',
                          variant='repaired', case_ids=['not-a-case'])
    review_file = tmp_path / 'review.json'
    review_file.write_text(json.dumps({'sample_sha256': file_hash(sample_file), 'cases': [],
                                      'frozen_repair_signature': adapter.repair_signature()}))
    with pytest.raises(ValueError, match='every frozen case'):
        adapter.run_model(sample, sample_file, tmp_path / 'missing-labels', operation='extraction',
                          variant='repaired', review_path=review_file, reserved=True)
