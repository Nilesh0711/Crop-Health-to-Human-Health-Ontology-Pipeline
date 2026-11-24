"""Offline regression tests for Phase 1 closed-relation extraction and Phase 2 entity-only grounding."""
from copy import deepcopy
from dataclasses import asdict
import json

from pypdf import PdfWriter
import pytest

from backend.cli import repair_sample as adapter
from backend.cli.appendix_examples import examples
from backend.config.settings import Settings
from backend.domain.classification import (
    classification_complete,
    classification_counts,
    endpoint_accepted,
    relation_accepted,
)
from backend.domain.extraction_review import assertion_eligible, extraction_warnings, review_status
from backend.ontology.catalog import catalog
from backend.ontology.inventory import open_inventory
from backend.ontology.policies import ENTITY_GROUNDING_POLICY
from backend.providers.structured_output import SchemaValidationError
from backend.shared.storage import file_hash
from backend.stages.claim_extraction.contract import (
    CLOSED_EXTRACTION_CONTRACT_VERSION,
    CLOSED_EXTRACTION_RESPONSE_SCHEMA,
    CLOSED_PREDICATE_SET_VERSION,
    CLOSED_PREDICATES,
    CLOSED_PREDICATES_BY_IRI,
    EXTRACTION_CONTRACT_VERSION,
    render_closed_predicate_glossary,
    verify_closed_predicates,
)
from backend.stages.claim_extraction.extractor import check_extraction_response, extract_claims
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import build_extraction_targets
from backend.stages.ontology_grounding import rows as wire_rows
from backend.stages.ontology_grounding.contract import (
    ENTITY_SYSTEM,
    check_response,
    packet_schema,
)
from backend.stages.ontology_grounding.grounder import catalog_term_map, ground_claims, request_packet

BASE = 'http://purl.obolibrary.org/obo/'


@pytest.fixture(scope='module')
def ref():
    return catalog()


def _make_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(path)


def test_closed_predicate_catalog_verification_and_abstract_prompt(ref):
    verified = verify_closed_predicates(ref)
    assert set(verified) == {f'P{i}' for i in range(1, 13)}
    assert BASE + 'RO_0019000' not in CLOSED_PREDICATES_BY_IRI
    assert all(CLOSED_PREDICATES[alias]['iri'].endswith(entry['id'].replace(':', '_'))
               for alias, entry in verified.items())

    glossary = render_closed_predicate_glossary(ref)
    for alias, entry in verified.items():
        assert alias in glossary
        assert entry['id'] in glossary
    assert 'RO:0019000' not in glossary

    default_prompt = build_extraction_prompt(0, 'target_context')
    assert EXTRACTION_CONTRACT_VERSION == 20
    assert 'RO:0019001' not in default_prompt

    closed_prompt = build_extraction_prompt(0, 'target_context', closed_relations=True, reference=ref)
    assert CLOSED_EXTRACTION_CONTRACT_VERSION == 26
    assert CLOSED_PREDICATE_SET_VERSION in closed_prompt
    assert 'RO:0019001' in closed_prompt
    assert 'RO:0003302' in closed_prompt
    assert 'RO:0002213' in closed_prompt
    assert 'RO:0002212' in closed_prompt
    assert 'BFO:0000051' in closed_prompt
    assert 'supports the full property' in closed_prompt
    assert 'dietary exposure' in closed_prompt and 'no free-text relation field' in closed_prompt
    assert 'measured_comparison' not in closed_prompt
    for forbidden in ('Chea', 'Kirchgesser', 'Pandey', 'Hu', 'Lampen'):
        assert forbidden not in closed_prompt


@pytest.mark.parametrize(
    'sentence,item,expected',
    [
        (
            'Organism A synthesized Compound B.',
            {'subject': 'Organism A', 'relation': 'synthesized', 'object': 'Compound B',
             'predicate_alias': 'P6', 'relation_anchor': 'synthesized', 'qualifiers': {}, 'evidence_sentence_ids': [1]},
            {'iri': BASE + 'RO_0003000', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Organ O was responsible for the delivery of Compound B.',
            {'subject': 'Organ O', 'relation': 'delivery of', 'object': 'Compound B',
             'predicate_alias': 'P6', 'relation_anchor': 'delivery of', 'qualifiers': {}, 'evidence_sentence_ids': [1]},
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Process P increased Trait Q intensity by 15% under low nutrient supply.',
            {
                'subject': 'Process P', 'relation': 'increased', 'predicate_alias': 'P1',
                'object': 'Trait Q intensity',
                'qualifiers': {'condition': 'low nutrient supply', 'metric': '15%'},
                'subject_anchor': 'Process P', 'relation_anchor': 'increased',
                'object_anchor': 'Trait Q intensity', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019001', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Variable X showed a significant negative correlation with Trait Y across treatments.',
            {
                'subject': 'Variable X', 'relation': 'correlated with', 'predicate_alias': 'P5',
                'object': 'Trait Y', 'qualifiers': {'condition': 'across treatments', 'metric': 'significant negative correlation'},
                'subject_anchor': 'Variable X', 'relation_anchor': 'showed a significant negative correlation with',
                'object_anchor': 'Trait Y', 'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0002610', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Variable X negatively correlated with Trait Y across treatments.',
            {
                'subject': 'Variable X', 'relation': 'negatively correlated with', 'predicate_alias': 'P5',
                'object': 'Trait Y', 'qualifiers': {},
                'subject_anchor': 'Variable X', 'relation_anchor': 'negatively correlated with',
                'object_anchor': 'Trait Y', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'non_exact_closed_relation', 'detail': 'negative correlation sign'},
        ),
        (
            'Organ O consists of Part R.',
            {
                'subject': 'Organ O', 'relation': 'consists of', 'predicate_alias': 'P7',
                'object': 'Part R', 'qualifiers': {},
                'subject_anchor': 'Organ O', 'relation_anchor': 'consists of',
                'object_anchor': 'Part R', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'BFO_0000051', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Pathway Z has input Substrate S and produces Product R.',
            {
                'subject': 'Pathway Z', 'relation': 'has input', 'predicate_alias': 'P8',
                'object': 'Substrate S', 'qualifiers': {},
                'subject_anchor': 'Pathway Z', 'relation_anchor': 'has input',
                'object_anchor': 'Substrate S', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0002233', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Pathway Z produces Product R during development.',
            {
                'subject': 'Pathway Z', 'relation': 'has output', 'predicate_alias': 'P9',
                'object': 'Product R', 'qualifiers': {},
                'subject_anchor': 'Pathway Z', 'relation_anchor': 'produces',
                'object_anchor': 'Product R', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0002234', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Trait Q was decreased by Process P under stress.',
            {
                'subject': 'Process P', 'relation': 'was decreased by', 'predicate_alias': 'P2',
                'object': 'Trait Q', 'qualifiers': {'condition': 'under stress'},
                'subject_anchor': 'Process P', 'relation_anchor': 'was decreased by',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019002', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': True, 'passive_ok': True,
            },
        ),
        (
            'Trait Q was decreased by Process P under stress.',
            {
                'subject': 'Trait Q', 'relation': 'was decreased by', 'predicate_alias': 'P2',
                'object': 'Process P', 'qualifiers': {'condition': 'under stress'},
                'subject_anchor': 'Trait Q', 'relation_anchor': 'was decreased by',
                'object_anchor': 'Process P', 'evidence_sentence_ids': [1],
            },
            {
                'rejection': 'unsupported_closed_relation',
                'warnings': {'possible_passive_role_reversal_failure'},
            },
        ),
        (
            'Trait Q rose as Factor T increased.',
            {
                'subject': 'Factor T', 'relation': 'rose as', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {},
                'subject_anchor': 'Factor T', 'relation_anchor': 'rose',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Treatment T decreased Trait Q by 18% under Condition C.',
            {
                'subject': 'Treatment T', 'relation': 'decreased', 'predicate_alias': 'P2',
                'object': 'Trait Q', 'qualifiers': {'condition': 'Condition C', 'metric': '18%'},
                'subject_anchor': 'Treatment T', 'relation_anchor': 'decreased',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019002', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Trait Q was reduced by Treatment T.',
            {
                'subject': 'Treatment T', 'relation': 'decreases', 'predicate_alias': 'P2',
                'object': 'Trait Q', 'qualifiers': {},
                'subject_anchor': 'Treatment T', 'relation_anchor': 'was reduced by',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019002', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': True, 'passive_ok': True,
            },
        ),
        (
            'Combined Treatment T and Treatment U increased root-to-shoot ratio by 15%.',
            {
                'subject': 'Combined Treatment T and Treatment U', 'relation': 'increases', 'predicate_alias': 'P1',
                'object': 'root-to-shoot ratio', 'qualifiers': {'metric': '15%'},
                'subject_anchor': 'Combined Treatment T and Treatment U', 'relation_anchor': 'increased',
                'object_anchor': 'root-to-shoot ratio', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019001', 'mapping': 'exact', 'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Trait Q was significantly affected by treatment levels.',
            {
                'subject': 'treatment levels', 'relation': 'significantly affected', 'predicate_alias': 'P3',
                'object': 'Trait Q', 'qualifiers': {},
                'subject_anchor': 'treatment levels', 'relation_anchor': 'significantly affected',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Treatment T improved Trait Q and Trait R under Condition C.',
            {
                'subject': 'Treatment T', 'relation': 'improved', 'predicate_alias': 'P1',
                'object': 'Trait Q and Trait R', 'qualifiers': {'condition': 'Condition C'},
                'subject_anchor': 'Treatment T', 'relation_anchor': 'improved',
                'object_anchor': 'Trait Q and Trait R', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019001', 'mapping': 'exact',
                'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Treatment T increased root length and root surface area.',
            {
                'subject': 'Treatment T', 'relation': 'increased', 'predicate_alias': 'P1',
                'object': 'root length and root surface area', 'qualifiers': {},
                'subject_anchor': 'Treatment T', 'relation_anchor': 'increased',
                'object_anchor': 'root length and root surface area', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'extraction_quality_rejected', 'warnings': {'possible_bundled_outcomes'}},
        ),
        (
            'Process P affected Trait Q across trials.',
            {
                'subject': 'Trait Q', 'relation': 'affected', 'predicate_alias': 'P3',
                'object': 'Process P', 'qualifiers': {},
                'subject_anchor': 'Trait Q', 'relation_anchor': 'affected',
                'object_anchor': 'Process P', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_agent_role_reversal'}},
        ),
        (
            'Agent A enhanced Trait Q under Condition C.',
            {
                'subject': 'Agent A', 'relation': 'enhances', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {'condition': 'Condition C'},
                'subject_anchor': 'Agent A', 'relation_anchor': 'enhanced',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'extraction_quality_rejected', 'warnings': {'possible_non_process_subject'}},
        ),
        (
            'Inoculation with Agent A augmented Trait Q in Group G more prominently than Group H.',
            {
                'subject': 'Inoculation with Agent A', 'relation': 'augments', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {'population': 'Group G'},
                'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'augmented',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'extraction_quality_rejected', 'warnings': {'possible_dropped_comparator'}},
        ),
        (
            'Inoculation with Agent A augmented Trait Q in Group G more prominently than Group H.',
            {
                'subject': 'Inoculation with Agent A', 'relation': 'augments', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {'population': 'Group G', 'comparator': 'Group H'},
                'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'augmented',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {
                'iri': BASE + 'RO_0019001', 'mapping': 'exact',
                'warnings': set(),
                'passive_evidence': False, 'passive_ok': None,
            },
        ),
        (
            'Trait Q decreased under Condition C.',
            {
                'subject': 'Condition C', 'relation': 'decreases', 'predicate_alias': 'P2',
                'object': 'Trait Q', 'qualifiers': {'condition': 'Condition C'},
                'subject_anchor': 'Condition C', 'relation_anchor': 'decreased',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'We saw higher Trait Q in Condition A.',
            {
                'subject': 'Condition A', 'relation': 'saw higher', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {'condition': 'Condition A'},
                'subject_anchor': 'Condition A', 'relation_anchor': 'saw higher',
                'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Both Trait Q and Trait R decreased under Regime C, which indicates an effect on Process P.',
            {'subject': 'Regime C', 'object': 'Trait R', 'predicate_alias': 'P2',
             'relation_anchor': 'Both Trait Q and Trait R decreased under Regime C',
             'qualifiers': {'condition': 'Regime C'}, 'evidence_sentence_ids': [1]},
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'},
             'message': 'An observed change under a condition'},
        ),
        (
            'We saw higher Trait Q as well as greater Trait R in Regime C.',
            {'subject': 'Regime C', 'object': 'Trait Q', 'predicate_alias': 'P1',
             'relation_anchor': 'higher Trait Q as well as greater Trait R in Regime C',
             'qualifiers': {'condition': 'Regime C'}, 'evidence_sentence_ids': [1]},
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'},
             'message': 'An observed change under a condition'},
        ),
        (
            'Under Regime C, Trait Q decreased.',
            {'subject': 'Regime C', 'object': 'Trait Q', 'predicate_alias': 'P2',
             'relation_anchor': 'decreased', 'qualifiers': {'condition': 'Regime C'}, 'evidence_sentence_ids': [1]},
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'},
             'message': 'An observed change under a condition'},
        ),
        (
            'During Treatment T, Trait R remained stable; Treatment T decreased Trait Q.',
            {'subject': 'Treatment T', 'object': 'Trait Q', 'predicate_alias': 'P2',
             'relation_anchor': 'decreased', 'qualifiers': {}, 'evidence_sentence_ids': [1]},
            {'iri': BASE + 'RO_0019002', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Treatment T led to an elevation in Trait Q.',
            {
                'subject': 'Treatment T', 'relation': 'increases', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {}, 'relation_anchor': 'led to an elevation in',
                'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0019001', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Under limited nutrient supply, Trait Q in Group A surpassed that in Group B.',
            {
                'subject': 'limited nutrient supply', 'relation': 'increases', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {'population': 'Group A', 'comparator': 'Group B'},
                'relation_anchor': 'surpassed', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Treatment T can stimulate Trait Q.',
            {
                'subject': 'Treatment T', 'relation': 'stimulates', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {}, 'relation_anchor': 'stimulate',
                'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'An increase was observed in Trait Q with an increment in Factor F level.',
            {
                'subject': 'increment in Factor F level', 'relation': 'increases', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {}, 'relation_anchor': 'An increase was observed in',
                'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Variable V positively correlated with Trait Q.',
            {
                'subject': 'Variable V', 'relation': 'correlated with', 'predicate_alias': 'P5',
                'object': 'Trait Q', 'qualifiers': {}, 'relation_anchor': 'correlated with',
                'evidence_sentence_ids': [1],
            },
            {'rejection': 'non_exact_closed_relation', 'detail': 'positive correlation sign'},
        ),
        (
            'Variable V correlated with Trait Q.',
            {
                'subject': 'Variable V', 'relation': 'correlated with', 'predicate_alias': 'P5',
                'object': 'Trait Q', 'qualifiers': {}, 'relation_anchor': 'correlated with',
                'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0002610', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Excess exposure E causes condition C.',
            {
                'subject': 'Excess exposure E', 'relation': 'causes', 'predicate_alias': 'P10',
                'object': 'condition C', 'qualifiers': {}, 'subject_anchor': 'Excess exposure E',
                'relation_anchor': 'causes', 'object_anchor': 'condition C', 'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0003302', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Excess exposure E may cause condition C.',
            {
                'subject': 'Excess exposure E', 'relation': 'may cause', 'predicate_alias': 'P10',
                'object': 'condition C', 'qualifiers': {}, 'subject_anchor': 'Excess exposure E',
                'relation_anchor': 'may cause', 'object_anchor': 'condition C', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
        (
            'Treatment T positively regulated Process P.',
            {
                'subject': 'Treatment T', 'relation': 'positively regulates', 'predicate_alias': 'P11',
                'object': 'Process P', 'qualifiers': {}, 'subject_anchor': 'Treatment T',
                'relation_anchor': 'positively regulated', 'object_anchor': 'Process P', 'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0002213', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Treatment T decreased Process P execution.',
            {
                'subject': 'Treatment T', 'relation': 'negatively regulates', 'predicate_alias': 'P12',
                'object': 'Process P', 'qualifiers': {}, 'subject_anchor': 'Treatment T',
                'relation_anchor': 'decreased', 'object_anchor': 'Process P', 'evidence_sentence_ids': [1],
            },
            {'iri': BASE + 'RO_0002212', 'mapping': 'exact', 'warnings': set(),
             'passive_evidence': False, 'passive_ok': None},
        ),
        (
            'Treatment T decreased Process P execution.',
            {
                'subject': 'Treatment T', 'relation': 'positively regulates', 'predicate_alias': 'P11',
                'object': 'Process P', 'qualifiers': {}, 'subject_anchor': 'Treatment T',
                'relation_anchor': 'decreased', 'object_anchor': 'Process P', 'evidence_sentence_ids': [1],
            },
            {'rejection': 'unsupported_closed_relation', 'warnings': {'possible_unsupported_closed_relation'}},
        ),
    ],
)
def test_closed_extraction_controls_and_role_checks(tmp_path, ref, sentence, item, expected):
    pdf = tmp_path / 'abstract.pdf'
    _make_pdf(pdf)
    packet = build_extraction_targets([{
        'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
        'word_count': len(sentence.split()), 'estimated_token_count': 40,
        'sentences': [{'text': sentence, 'page': 1}],
    }], 200)[0]

    class Client:
        def chat_json(self, system, user, **kwargs):
            answer = {'claims': [{key: value for key, value in item.items() if key != 'relation'}]}
            kwargs['response_check'](answer)
            return answer

    candidates, rejected, diagnostics = extract_claims(
        pdf, Client(), 0, lambda *a: None, packets=[packet], prepared_targets=True,
        extraction_mode='target_context', target_words=200, bibliography={},
        closed_relations=True, reference=ref,
    )
    if 'rejection' in expected:
        assert not candidates
        assert rejected == {expected['rejection']: 1}
        assert diagnostics[0]['source_evidence_span'] == sentence
        proposed = diagnostics[0]['proposed_claim']
        assert expected.get('warnings', set()) <= {w['code'] for w in proposed.get('extraction_warnings', [])}
        if 'message' in expected:
            assert any(expected['message'] in w['message'] for w in proposed['extraction_warnings'])
        if 'detail' in expected:
            assert expected['detail'] in proposed['proposed_relation_detail_loss']
        return
    assert rejected == {}
    assert len(candidates) == 1
    claim = candidates[0]
    assert claim['extraction_contract_version'] == CLOSED_EXTRACTION_CONTRACT_VERSION
    assert claim['predicate_set_version'] == CLOSED_PREDICATE_SET_VERSION
    assert claim['proposed_relation_iri'] == expected['iri']
    assert claim['proposed_relation_mapping'] == expected['mapping']
    assert claim['relation'] == CLOSED_PREDICATES[item['predicate_alias']]['display_label']
    assert claim['relation_trigger'] == item['relation_anchor']
    assert claim.get('passive_source_evidence') == expected['passive_evidence']
    assert claim.get('passive_role_reversal_verified') is expected['passive_ok']
    warns = {w['code'] for w in extraction_warnings(claim)}
    assert expected['warnings'] <= warns


def test_closed_extraction_abstains_and_rejects_malformed_or_context_only_anchors(tmp_path, ref):
    pdf = tmp_path / 'controls.pdf'
    _make_pdf(pdf)
    packets = build_extraction_targets([{
        'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
        'word_count': 24, 'estimated_token_count': 60,
        'sentences': [
            {'text': 'Process P increased Trait Q in preliminary tests.', 'page': 1},
            {'text': 'Variable X differed between Group A and Group B.', 'page': 1},
        ],
    }], 10)[0]
    # Make a 2-sentence target where sentence 1 is context-only and sentence 2 is target.
    target = {
        **packets,
        'target_id': 'target-s2',
        'target_sentence_numbers': [2],
        'reading_context_sentence_numbers': [1],
        'sentences': [
            {'text': 'Process P increased Trait Q in preliminary tests.', 'page': 1},
            {'text': 'Variable X differed between Group A and Group B.', 'page': 1},
        ],
    }

    with pytest.raises(SchemaValidationError):
        check_extraction_response(
            {'claims': [{
                'subject': 'Variable X', 'predicate_alias': 'P99',
                'relation_anchor': 'differed', 'object': 'Group A', 'qualifiers': {}, 'evidence_sentence_ids': [2],
            }]},
            CLOSED_EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2],
        )

    with pytest.raises(SchemaValidationError):
        check_extraction_response(
            {'claims': [{
                'subject': 'Process P', 'predicate_alias': 'P1',
                'object': 'Trait Q', 'qualifiers': {}, 'evidence_sentence_ids': [2],
            }]},
            CLOSED_EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2],
        )

    class AbstainClient:
        def chat_json(self, system, user, **kwargs):
            answer = {'claims': []}
            kwargs['response_check'](answer)
            return answer

    candidates, rejected, _ = extract_claims(
        pdf, AbstainClient(), 0, lambda *a: None, packets=[target], prepared_targets=True,
        extraction_mode='target_context', target_words=200, bibliography={},
        closed_relations=True, reference=ref,
    )
    assert candidates == [] and rejected == {}

    class BadClaimsClient:
        def chat_json(self, system, user, **kwargs):
            return {'claims': [
                {
                    'subject': 'Process P', 'predicate_alias': 'P99',
                    'object': 'Trait Q', 'qualifiers': {},
                    'subject_anchor': 'Process P', 'relation_anchor': 'increased',
                    'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1, 2],
                },
                {
                    'subject': 'Variable X', 'predicate_alias': 'P1',
                    'object': 'Group A', 'qualifiers': {},
                    'subject_anchor': 'Variable X', 'relation_anchor': 'increased',
                    'object_anchor': 'Group A', 'evidence_sentence_ids': [1, 2],
                },
            ]}

    candidates, rejected, records = extract_claims(
        pdf, BadClaimsClient(), 0, lambda *a: None, packets=[target], prepared_targets=True,
        extraction_mode='target_context', target_words=200, bibliography={},
        closed_relations=True, reference=ref,
    )
    assert candidates == []
    assert rejected.get('malformed_predicate_alias') == 1
    assert rejected.get('unanchored_evidence') == 1
    unanchored = next(r for r in records if r['reason'] == 'unanchored_evidence')
    assert any(f['category'] == 'context_only_relation_anchor' for f in unanchored['anchor_failures'])


def test_entity_only_grounding_packets_schemas_and_type_checks(tmp_path, ref):
    inventory = open_inventory()
    claim = {
        'candidate_id': 'cand-p1-good',
        'source_document_id': 'doc',
        'source_pdf_sha256': 'sha',
        'source_page': 1,
        'subject': 'phosphorus uptake',
        'relation': 'increased',
        'object': 'shoot dry weight',
        'predicate_alias': 'P1',
        'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
        'proposed_relation_iri': BASE + 'RO_0019001',
        'proposed_relation_id': 'RO:0019001',
        'proposed_relation_display_label': CLOSED_PREDICATES['P1']['display_label'],
        'proposed_relation_mapping': 'exact',
        'qualifiers': {},
        'source_evidence_span': 'Phosphorus uptake increased shoot dry weight.',
        'source_evidence_sentence_numbers': [1],
        'source_evidence_fragments': [{'sentence_number': 1, 'text': 'Phosphorus uptake increased shoot dry weight.', 'page': 1}],
        'subject_source_mention': 'phosphorus uptake',
        'object_source_mention': 'shoot dry weight',
        'relation_trigger': 'increased',
    }
    payload, check = request_packet([claim], ref, inventory=inventory)
    assert payload['entity_only'] is True
    c1 = payload['claims']['C1']
    assert 'relation_candidates' not in c1
    assert c1['fixed_property']['alias'] == 'P1'
    assert c1['fixed_property']['iri'] == BASE + 'RO_0019001'
    assert 'domain_compatible_subject_candidates' in c1
    assert 'range_compatible_object_candidates' in c1
    assert c1['verified_relation_endpoint_matches'] == {
        'subject': c1['domain_compatible_subject_candidates'],
        'object': c1['range_compatible_object_candidates'],
    }
    assert 'relation' not in packet_schema(payload)['properties']['results']['items']['properties']

    subj_t = c1['subject_candidates'][0]
    obj_t = c1['object_candidates'][0]
    valid_json = {
        'results': [{
            'claim_id': 'C1', 'scope': 'in_scope', 'scope_reason': 'Crop nutrition fits scope.',
            'subject_denotation': 'concept', 'object_denotation': 'concept',
            'subject': {'reason': 'Matches subject concept.', 'mapping': 'broader', 'term': subj_t},
            'object': {'reason': 'Matches object concept.', 'mapping': 'broader', 'term': obj_t},
        }],
    }
    assert check(valid_json) is True
    with pytest.raises(SchemaValidationError):
        check({
            'results': [{
                **valid_json['results'][0],
                'relation': {'reason': 'Disallowed relation field.', 'mapping': 'exact', 'term': 'R1'},
            }],
        })

    valid_tsv = '\n'.join([
        'C1.scope\tscope\tin_scope\t-\tCrop nutrition fits scope.',
        f'C1.subject\tconcept\tbroader\t{subj_t}\tMatches subject concept.',
        f'C1.object\tconcept\tbroader\t{obj_t}\tMatches object concept.',
    ])
    assert wire_rows.parse_rows(valid_tsv, payload) == valid_json
    with pytest.raises(SchemaValidationError):
        wire_rows.parse_rows(valid_tsv + '\nC1.relation\trelation\texact\tR1\tExtra.', payload)

    # Static catalog run verifying compatible (P7), incompatible (P1 continuant->quality), insufficient (P1 GO_0003993), and failed passive reversal.
    _, terms, term_iris, _, _ = catalog_term_map(ref)
    static_payload, _ = request_packet([claim], ref)
    static_iris = {iri: alias for alias, iri in term_iris.items() if alias in static_payload['terms']}

    claims_to_ground = [
        {
            'candidate_id': 'valid-p7', 'source_document_id': 'doc', 'source_pdf_sha256': 'sha', 'source_page': 1,
            'subject': 'root system', 'relation': 'consists of', 'object': 'root',
            'predicate_alias': 'P7', 'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
            'proposed_relation_iri': BASE + 'BFO_0000051', 'proposed_relation_mapping': 'exact',
            'qualifiers': {}, 'source_evidence_span': 'A root system consists of a root.',
            'source_evidence_sentence_numbers': [1],
            'source_evidence_fragments': [{'sentence_number': 1, 'text': 'A root system consists of a root.', 'page': 1}],
            'subject_source_mention': 'root system', 'object_source_mention': 'root', 'relation_trigger': 'consists of',
        },
        {
            'candidate_id': 'conflict-p1', 'source_document_id': 'doc', 'source_pdf_sha256': 'sha', 'source_page': 1,
            'subject': 'root', 'relation': 'increased', 'object': 'concentration',
            'predicate_alias': 'P1', 'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
            'proposed_relation_iri': BASE + 'RO_0019001', 'proposed_relation_mapping': 'exact',
            'qualifiers': {}, 'source_evidence_span': 'Root increased concentration.',
            'source_evidence_sentence_numbers': [1],
            'source_evidence_fragments': [{'sentence_number': 1, 'text': 'Root increased concentration.', 'page': 1}],
            'subject_source_mention': 'root', 'object_source_mention': 'concentration', 'relation_trigger': 'increased',
        },
        {
            'candidate_id': 'insufficient-p1', 'source_document_id': 'doc', 'source_pdf_sha256': 'sha', 'source_page': 1,
            'subject': 'acid phosphatase activity', 'relation': 'increased', 'object': 'concentration',
            'predicate_alias': 'P1', 'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
            'proposed_relation_iri': BASE + 'RO_0019001', 'proposed_relation_mapping': 'exact',
            'qualifiers': {}, 'source_evidence_span': 'Acid phosphatase activity increased concentration.',
            'source_evidence_sentence_numbers': [1],
            'source_evidence_fragments': [{'sentence_number': 1, 'text': 'Acid phosphatase activity increased concentration.', 'page': 1}],
            'subject_source_mention': 'acid phosphatase activity', 'object_source_mention': 'concentration', 'relation_trigger': 'increased',
        },
    ]
    choices_by_cid = {
        'valid-p7': (static_iris[BASE + 'PO_0025025'], static_iris[BASE + 'PO_0009005']),
        'conflict-p1': (static_iris[BASE + 'PO_0009005'], static_iris[BASE + 'PATO_0000033']),
        'insufficient-p1': (static_iris[BASE + 'GO_0003993'], static_iris[BASE + 'PATO_0000033']),
    }
    by_pair = {
        (c['subject'], c['object']): choices_by_cid[c['candidate_id']]
        for c in claims_to_ground
    }

    class FakeEntityClient:
        settings = Settings()
        model = 'test-entity-grounder'

        def chat_json(self, system, user, **kwargs):
            assert system == ENTITY_SYSTEM
            req = json.loads(user)
            assert req['entity_only'] is True
            results = []
            for key, item in req['claims'].items():
                s_term, o_term = by_pair[(item['subject'], item['object'])]
                s_map = 'broader' if item['subject'] == 'acid phosphatase activity' else 'exact'
                results.append({
                    'claim_id': key, 'scope': 'in_scope', 'scope_reason': 'Fits scope.',
                    'subject_denotation': 'concept', 'object_denotation': 'concept',
                    'subject': {'reason': 'Matches subject.', 'mapping': s_map, 'term': s_term},
                    'object': {'reason': 'Matches object.', 'mapping': 'exact', 'term': o_term},
                })
            ans = {'results': results}
            kwargs['response_check'](ans)
            return ans

    rows = ground_claims(claims_to_ground, tmp_path / 'ground_out', ref, FakeEntityClient(), batch_size=1)
    by_cid = {r['candidate_id']: r for r in rows}
    selected_rows = ground_claims(claims_to_ground[:1], tmp_path / 'ground_out', ref, FakeEntityClient(),
                                  batch_size=1, preserve_unselected=True)
    assert [row['candidate_id'] for row in selected_rows] == ['valid-p7']
    saved_rows = [json.loads(line) for line in (tmp_path / 'ground_out/ontology_grounding.jsonl').read_bytes().splitlines()]
    assert {row['candidate_id'] for row in saved_rows} == set(by_cid)

    assert by_cid['valid-p7']['policy'] == ENTITY_GROUNDING_POLICY
    assert by_cid['valid-p7']['bindings']['relation']['iri'] == BASE + 'BFO_0000051'
    assert relation_accepted(by_cid['valid-p7'], ref) is True
    assert classification_complete(by_cid['valid-p7'], ref) is True
    assert assertion_eligible(by_cid['valid-p7'], ref) is True

    conflict = by_cid['conflict-p1']
    assert conflict['property_type_check']['status'] == 'incompatible'
    assert conflict['bindings']['relation']['under_review'] is True
    assert endpoint_accepted(conflict, 'subject', ref) is True
    assert endpoint_accepted(conflict, 'object', ref) is True
    assert relation_accepted(conflict, ref) is False
    assert assertion_eligible(conflict, ref) is False

    insuff = by_cid['insufficient-p1']
    assert insuff['property_type_check']['status'] == 'insufficient_type_info'
    assert insuff['bindings']['relation']['under_review'] is True
    assert endpoint_accepted(insuff, 'subject', ref) is True
    assert endpoint_accepted(insuff, 'object', ref) is True
    assert relation_accepted(insuff, ref) is False
    assert assertion_eligible(insuff, ref) is False

    counts = classification_counts(rows, ref)
    assert counts['classified_endpoint_mentions'] == 6
    assert counts['classified_relation_mentions'] == 1
    assert counts['proposed_relation_under_review_mentions'] == 2


def test_appendix_includes_closed_extraction_and_entity_grounding(ref):
    data = examples()
    ce = data['closedExtractionExample']
    check_extraction_response(ce['output'], CLOSED_EXTRACTION_RESPONSE_SCHEMA, [1], [1])
    eg = data['entityGroundingExample']
    assert eg['input']['entity_only'] is True
    check_response(eg['output'], eg['input'])
    result = eg['output']['results'][0]
    assert 'relation' not in result
    tsv = '\n'.join([
        f"C1.scope\tscope\tin_scope\t-\t{result['scope_reason']}",
        f"C1.subject\tconcept\texact\t{result['subject']['term']}\t{result['subject']['reason']}",
        f"C1.object\tconcept\texact\t{result['object']['term']}\t{result['object']['reason']}",
    ])
    assert wire_rows.parse_rows(tsv, eg['input']) == eg['output']
    with pytest.raises(SchemaValidationError):
        wire_rows.parse_rows(tsv + '\nC1.relation\trelation\texact\tR1\tForbidden.', eg['input'])


def test_closed_extraction_rejects_untyped_observations_and_free_text(tmp_path, ref):
    pdf = tmp_path / 'abstract.pdf'
    _make_pdf(pdf)
    sentence = 'Group A did not differ significantly in root length from Group B.'
    packet = {'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
              'word_count': 12, 'estimated_token_count': 25, 'sentences': [{'text': sentence, 'page': 1}]}
    finding = {'subject': 'Group A', 'object': 'root length', 'relation': 'did not differ significantly',
               'relation_anchor': 'did not differ significantly', 'evidence_sentence_ids': [1],
               'qualifiers': {'comparator': 'Group B', 'metric': 'not significant'},
               'measured_comparison': {'comparison_axis': 'group', 'direction': 'no_significant_difference',
                                       'statistical_significance': 'not_significant', 'comparator_quote': 'Group B'}}
    invalid = {**finding, 'predicate_alias': 'P1'}
    with pytest.raises(SchemaValidationError):
        check_extraction_response({'claims': [invalid]}, CLOSED_EXTRACTION_RESPONSE_SCHEMA, [1], [1])

    class Extractor:
        def chat_json(self, *_args, **kwargs):
            free_text = {key: value for key, value in invalid.items() if key != 'measured_comparison'}
            answer = {'claims': [finding, free_text]}
            kwargs['response_check'](answer)
            return answer

    extracted, rejected, records = extract_claims(pdf, Extractor(), 0, lambda *_: None, packets=[packet],
                                                  closed_relations=True, reference=ref, bibliography={})
    assert not extracted and rejected == {'invalid_finding_kind': 2}
    assert all(record['source_evidence_span'] == sentence for record in records)


def test_duplicate_anchor_and_untyped_rows_do_not_abort_closed_extraction(tmp_path, ref, monkeypatch):
    from backend.providers.llm import OpenCodeClient
    from backend.providers.structured_output import DuplicateJSONPropertyError, parse_json_object
    from backend.shared.artifacts import save_rows
    from backend.stages.claim_extraction.extractor import _load_cached_packet_claims

    pdf = tmp_path / 'abstract.pdf'
    _make_pdf(pdf)
    sentence = 'A root system consists of a root.'
    packet = {'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
              'word_count': 8, 'estimated_token_count': 20, 'sentences': [{'text': sentence, 'page': 1}]}
    claim = {'subject': 'root system', 'object': 'root', 'relation_anchor': 'consists of',
             'predicate_alias': 'P7', 'qualifiers': {}, 'evidence_sentence_ids': [1]}
    untyped = {key: value for key, value in claim.items() if key != 'predicate_alias'}
    duplicate = json.dumps(claim)[:-1] + ', "relation_anchor": "consists of"}'
    raw = '{"claims": [' + duplicate + ', ' + json.dumps(untyped) + ']}'
    with pytest.raises(DuplicateJSONPropertyError, match='relation_anchor'):
        parse_json_object(raw)
    with pytest.raises(DuplicateJSONPropertyError, match='relation_anchor'):
        parse_json_object(raw.replace(', "relation_anchor": "consists of"}', ', "relation_anchor": "different"}', 1),
                          allow_identical_claim_anchors=True)

    requests, coverage = [], []
    client = OpenCodeClient(Settings(), task='extraction', on_request=requests.append,
                           response_dir=tmp_path / 'model_responses')
    monkeypatch.setattr(client._inference, 'chat', lambda *_args: {
        'content': raw, 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object',
    })
    extracted, rejected, rejections = extract_claims(pdf, client, 0, lambda *_: None,
        packets=[packet], packet_results=coverage, extraction_mode='target_context',
        closed_relations=True, reference=ref, bibliography={})
    assert len(requests) == 1 and len(extracted) == 1
    assert extracted[0]['relation_trigger'] == 'consists of'
    assert rejected == {'invalid_finding_kind': 1}
    assert rejections[0]['source_evidence_span'] == sentence
    artifact = next((tmp_path / 'model_responses').glob('*.json'))
    record = json.loads(artifact.read_bytes())
    assert record['content'] == raw and record['status'] == 'ok'
    assert record['format_adjustments'] == [{'kind': 'identical_duplicate_relation_anchor',
                                            'path': ['claims', 0, 'relation_anchor']}]
    assert record['predicate_set_version'] == CLOSED_PREDICATE_SET_VERSION
    save_rows(tmp_path / 'extraction_packets.jsonl', coverage)
    save_rows(tmp_path / 'llm_requests.jsonl', requests)
    artifact.with_name('unlogged.json').write_text(json.dumps({**record, 'accepted_response': {'claims': []}}),
                                                   encoding='utf-8')
    cached = _load_cached_packet_claims(tmp_path, 'target_context', contract_version=CLOSED_EXTRACTION_CONTRACT_VERSION,
                                        response_schema=CLOSED_EXTRACTION_RESPONSE_SCHEMA, closed_relations=True)
    assert list(cached.values()) == [[claim, untyped]]


def test_offline_adapter_reuses_closed_extraction_for_entity_grounding(tmp_path, monkeypatch, ref):
    root = tmp_path / 'work'
    (root / 'pdf').mkdir(parents=True)
    pdf = root / 'pdf/doc.pdf'
    _make_pdf(pdf)
    packet = {
        'packet_number': 1, 'section_heading': 'Result', 'page_start': 1, 'page_end': 1,
        'word_count': 8, 'estimated_token_count': 20,
        'sentences': [{'text': 'A root system consists of a root.', 'page': 1}],
    }
    target = build_extraction_targets([packet], 200)[0]
    baseline_claim = {
        'candidate_id': 'base-1', 'subject': 'root system', 'relation': 'consists of', 'object': 'root',
        'qualifiers': {}, 'source_target_id': target['target_id'],
        'source_evidence_sentence_numbers': [1],
        'source_evidence_span': 'A root system consists of a root.',
        'source_evidence_fragments': [{'sentence_number': 1, **packet['sentences'][0]}],
        'subject_source_mention': 'root system', 'object_source_mention': 'root',
    }
    sample = {
        'purpose': 'Two-stage closed-relation fixture',
        'policies': adapter.policies(),
        'settings': asdict(Settings()),
        'catalog_signature': ref.signature,
        'class_count': len(ref.profile['allowed_classes']),
        'property_count': len(ref.profile['allowed_object_properties']),
        'baseline_extraction_schema': CLOSED_EXTRACTION_RESPONSE_SCHEMA,
        'baseline_grounding_system': ENTITY_SYSTEM,
        'documents': {'doc': {
            'pdf_sha256': file_hash(pdf), 'document_id': 'doc', 'title': 'doc',
            'targets': [target], 'baseline_claims': [baseline_claim], 'baseline_rejections': [],
            'abbreviation_definitions': [], 'scope_instructions': ref.extraction_instructions(),
        }},
        'cases': [{
            'case_id': 'chea-01', 'partition': 'development', 'document': 'doc',
            'target_ids': [target['target_id']],
            'baseline_mappings': [{'original_claim': baseline_claim, 'bindings': {}, 'candidate_id': 'base-1'}],
            'baseline_rejections': [], 'expectations': 'Keep anatomical whole->part.',
            'baseline_target_compatible': True, 'baseline_policy_compatible': True, 'claim_ids': ['base-1'],
        }],
    }
    frozen = root / 'analysis/frozen'
    frozen.mkdir(parents=True)
    adapter.write_json(frozen / 'sample.json', sample)
    adapter.write_json(frozen / 'sample.sha256.json', {'sha256': file_hash(frozen / 'sample.json')})
    monkeypatch.setattr(adapter, 'PROJECT_ROOT', root)

    class FakeProvider:
        def __init__(self, settings, *, task='extraction', on_request=None, on_progress=None, response_dir=None):
            self.settings = settings
            self.task = task
            self.model = getattr(settings, f'{task}_model', 'fake-model')

        def chat_json(self, system, user, **kwargs):
            if self.task == 'extraction':
                ans = {'claims': [{
                    'subject': 'root system', 'predicate_alias': 'P7',
                    'object': 'root', 'qualifiers': {},
                    'subject_anchor': 'root system', 'relation_anchor': 'consists of',
                    'object_anchor': 'root', 'evidence_sentence_ids': [1],
                }]}
                kwargs['response_check'](ans)
                return ans
            req = json.loads(user)
            c1 = req['claims']['C1']
            s_term = next(k for k, v in req['terms'].items() if v['label'] == 'root system')
            o_term = next(k for k, v in req['terms'].items() if v['label'] == 'root')
            ans = {'results': [{
                'claim_id': 'C1', 'scope': 'in_scope', 'scope_reason': 'Plant anatomy fits scope.',
                'subject_denotation': 'concept', 'object_denotation': 'concept',
                'subject': {'reason': 'Exact root system.', 'mapping': 'exact', 'term': s_term},
                'object': {'reason': 'Exact root.', 'mapping': 'exact', 'term': o_term},
            }]}
            kwargs['response_check'](ans)
            return ans

    monkeypatch.setattr('backend.providers.llm.OpenCodeClient', FakeProvider)

    from backend.shared.storage import read_jsonl

    source, output = root / 'analysis/extraction', root / 'analysis/grounding'
    sample_path = frozen / 'sample.json'
    extraction = adapter.run_closed_extraction(sample, sample_path, source, cases=sample['cases'], reference=ref)
    assert extraction['claims'] == 1 and extraction['targets_evaluated'] == 1
    grounding = adapter.run_entity_grounding(sample, sample_path, output, cases=sample['cases'],
                                             reuse_extraction=source, reference=ref)
    assert grounding['claims'] == 1
    comparison = grounding['consistency_comparison']
    assert comparison['before'] == comparison['after']
    assert comparison['adjusted_endpoints'] == 0
    row, = read_jsonl(output / 'doc/grounding/ontology_grounding.jsonl')
    assert row['original_claim']['predicate_alias'] == 'P7'
    assert row['bindings']['relation']['iri'] == BASE + 'BFO_0000051'
    assert row['bindings']['relation']['fixed_from_extraction'] is True

    # Tampering with saved Stage 1 input invalidates checkpoint verification for Stage 2.
    inp = next((source / 'doc').glob('input-*.json'))
    inp.write_text(inp.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    with pytest.raises(ValueError, match='checkpoint changed or is incompatible'):
        adapter.run_entity_grounding(sample, sample_path, root / 'analysis/tampered', cases=sample['cases'],
                                     reuse_extraction=source, reference=ref)


def test_closed_extract_document_mode_and_multisource_process_subject(tmp_path, monkeypatch, ref):
    root = tmp_path / 'work'
    (root / 'pdf').mkdir(parents=True)
    pdf = root / 'pdf/doc.pdf'
    _make_pdf(pdf)
    packet = {
        'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
        'word_count': 42, 'estimated_token_count': 60,
        'sentences': [
            {'text': 'In Condition C, Group G responded to Agent A, augmenting Trait Q more prominently than Group H.', 'page': 1},
            {'text': 'Factor F levels significantly affected Growth G, while Inoculation with Agent A improved Organ O biomass.', 'page': 1},
        ],
    }
    target = build_extraction_targets([packet], 200)[0]
    sample = {
        'purpose': 'Full-document CLI and multi-sentence process subject fixture',
        'policies': adapter.policies(),
        'settings': asdict(Settings()),
        'catalog_signature': ref.signature,
        'class_count': len(ref.profile['allowed_classes']),
        'property_count': len(ref.profile['allowed_object_properties']),
        'baseline_extraction_schema': CLOSED_EXTRACTION_RESPONSE_SCHEMA,
        'baseline_grounding_system': ENTITY_SYSTEM,
        'documents': {'doc': {
            'pdf_sha256': file_hash(pdf), 'document_id': 'doc', 'title': 'doc',
            'targets': [target], 'baseline_claims': [], 'baseline_rejections': [],
            'abbreviation_definitions': [], 'scope_instructions': ref.extraction_instructions(),
        }},
        'cases': [{
            'case_id': 'doc-01', 'partition': 'development', 'document': 'doc',
            'target_ids': [target['target_id']],
            'baseline_mappings': [], 'baseline_rejections': [],
            'expectations': 'Retain process subject across cited sentences.',
            'baseline_target_compatible': True, 'baseline_policy_compatible': True, 'claim_ids': [],
        }],
    }
    sample_path = root / 'analysis/frozen/sample.json'
    sample_path.parent.mkdir(parents=True)
    adapter.write_json(sample_path, sample)
    adapter.write_json(sample_path.with_name('sample.sha256.json'), {'sha256': file_hash(sample_path)})
    monkeypatch.setattr(adapter, 'PROJECT_ROOT', root)

    class FakeClient:
        def __init__(self, settings, *, task='extraction', on_request=None, on_progress=None, response_dir=None):
            self.settings = settings
            self.model = settings.extraction_model

        def chat_json(self, system, user, **kwargs):
            ans = {'claims': [
                {
                    'subject': 'Inoculation with Agent A', 'predicate_alias': 'P1',
                    'object': 'Trait Q',
                    'qualifiers': {'population': 'Group G', 'condition': 'Condition C', 'comparator': 'Group H'},
                    'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'augmenting',
                    'object_anchor': 'Trait Q', 'evidence_sentence_ids': [1, 2],
                },
                {
                    'subject': 'Inoculation with Agent A', 'predicate_alias': 'P1',
                    'object': 'Organ O biomass', 'qualifiers': {},
                    'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'improved',
                    'object_anchor': 'Organ O biomass', 'evidence_sentence_ids': [2],
                },
            ]}
            kwargs['response_check'](ans)
            return ans

    monkeypatch.setattr('backend.providers.llm.OpenCodeClient', FakeClient)
    out_dir = root / 'analysis/doc_stage1'
    exit_code = adapter.main([
        'closed-extract', '--sample', str(sample_path), '--output', str(out_dir), '--document', 'doc',
    ])
    assert exit_code == 0
    report = json.loads((out_dir / 'report.json').read_text(encoding='utf-8'))
    assert report['claims'] == 2
    assert report['held'] == 0
    assert report['targets_evaluated'] == 1
    assert report['predicate_counts'] == {'P1': 2}
    saved = [json.loads(line) for line in (out_dir / 'doc/candidates.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    assert all(extraction_warnings(c) == [] for c in saved)
    assert saved[0]['qualifiers']['comparator'] == 'Group H'


def test_runs_api_keeps_historical_closed_claims_and_hold_reasons(tmp_path):
    from fastapi.testclient import TestClient
    from backend.pipelines.unvalidated import paused_metadata
    from backend.shared.artifacts import save_rows
    from backend.viewer.export import display_api

    pdf = tmp_path / 'Historical paper.pdf'
    _make_pdf(pdf)
    output = tmp_path / 'ontology' / pdf.stem
    output.mkdir(parents=True)
    warning = {'code': 'possible_unsupported_closed_relation', 'message': 'Check the source predicate.'}
    common = {'subject': 'Trait Q', 'relation': 'correlated with', 'object': 'Trait R',
              'predicate_alias': 'P5', 'proposed_relation_id': 'RO:0002610',
              'source_evidence_span': 'Trait Q showed a positive correlation with Trait R.',
              'source_page': 1, 'qualifiers': {'population': 'Group A'}, 'extraction_contract_version': 21}
    save_rows(output / 'candidates.jsonl', [
        {**common, 'candidate_id': 'saved-broader', 'proposed_relation_mapping': 'broader',
         'extraction_review_status': 'not_flagged', 'extraction_warnings': []},
        {**common, 'candidate_id': 'saved-held', 'proposed_relation_mapping': 'exact',
         'extraction_review_status': 'held', 'extraction_warnings': [warning]},
    ])
    (output / 'summary.json').write_text(json.dumps({
        **paused_metadata(), 'run_status': 'completed', 'extracted': 2, 'ready_to_inspect': 1,
        'held_for_review': 1, 'extraction_contract_version': 21, 'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
        'stage_status': {'ontology_grounding': 'paused'},
    }), encoding='utf-8')

    with display_api(output.parent, tmp_path, {'paper-id': pdf}) as api, TestClient(api.app) as client:
        papers = client.get('/api/pdfs').json()
        assert len(papers) == 1
        summary = papers[0]['summary']
        assert summary['extraction_contract_version'] == 21
        assert summary['predicate_set_version'] == CLOSED_PREDICATE_SET_VERSION
        assert summary['stage_status']['ontology_grounding'] == 'paused'
        assert summary['held_for_review'] == 1
        payload = client.get('/api/pdfs/paper-id/claims').json()
        assert payload['total'] == 2
        broader, held = [row['candidate'] for row in payload['claims']]
        assert broader['proposed_relation_mapping'] == 'broader'
        assert held['extraction_warnings'] == [warning]
        assert held['source_evidence_span'] == common['source_evidence_span']


@pytest.mark.parametrize(
    'sentence,item,expected_reason,expected_subject_mention',
    [
        (
            'Increasing the P level to P30 led to an elevation in root P concentration.',
            {
                'subject': 'phosphorus supplementation', 'predicate_alias': 'P1',
                'object': 'root phosphorus concentration',
                'qualifiers': {'condition': 'P30'},
                'subject_anchor': 'Increasing the P level to P30',
                'relation_anchor': 'led to an elevation in',
                'object_anchor': 'root P concentration',
                'evidence_sentence_ids': [1],
            },
            None,
            'Increasing the P level to P30',
        ),
        (
            'Cultivar Milva can allocate P to its roots under low P, enhancing phosphorus use efficiency.',
            {
                'subject': 'root phosphorus allocation', 'predicate_alias': 'P1',
                'object': 'phosphorus use efficiency',
                'qualifiers': {'population': 'Milva', 'condition': 'under low P'},
                'subject_anchor': 'allocate P to its roots',
                'relation_anchor': 'enhancing',
                'object_anchor': 'phosphorus use efficiency',
                'evidence_sentence_ids': [1],
            },
            None,
            'allocate P to its roots',
        ),
        (
            'Trait Q and Trait R changed significantly.',
            {
                'subject': 'invented effect', 'predicate_alias': 'P5',
                'object': 'Trait R', 'qualifiers': {},
                'subject_anchor': 'Trait Q', 'relation_anchor': 'changed',
                'object_anchor': 'Trait R', 'evidence_sentence_ids': [1],
            },
            'unanchored_evidence',
            None,
        ),
        (
            'Plants revealed a decline in biomass when exposed to both extremely low (0 mg P L-1) and high P levels (>= 30 mg P L-1).',
            {
                'subject': 'exposed to both extremely low (0 mg P L-1) and high P levels (>= 30 mg P L-1)',
                'predicate_alias': 'P2', 'object': 'plant biomass', 'qualifiers': {},
                'subject_anchor': 'exposed to both extremely low (0 mg P L-1) and high P levels (>= 30 mg P L-1)',
                'relation_anchor': 'revealed a decline in', 'object_anchor': 'biomass',
                'evidence_sentence_ids': [1],
            },
            'noisy_endpoint_rejected',
            None,
        ),
        (
            'Higher root dry matter and phosphorus uptake increased phosphorus uptake efficiency.',
            {
                'subject': 'higher root dry matter and phosphorus uptake',
                'predicate_alias': 'P1', 'object': 'phosphorus uptake efficiency', 'qualifiers': {},
                'subject_anchor': 'Higher root dry matter and phosphorus uptake',
                'relation_anchor': 'increased', 'object_anchor': 'phosphorus uptake efficiency',
                'evidence_sentence_ids': [1],
            },
            'noisy_endpoint_rejected',
            None,
        ),
    ],
)
def test_canonical_anchoring_and_noisy_endpoint_rejection(tmp_path, ref, sentence, item, expected_reason, expected_subject_mention):
    pdf = tmp_path / 'canonical.pdf'
    _make_pdf(pdf)
    packet = build_extraction_targets([{
        'packet_number': 1, 'section_heading': 'Results', 'page_start': 1, 'page_end': 1,
        'word_count': len(sentence.split()), 'estimated_token_count': 50,
        'sentences': [{'text': sentence, 'page': 1}],
    }], 200)[0]

    class Client:
        def chat_json(self, system, user, **kwargs):
            ans = {'claims': [item]}
            kwargs['response_check'](ans)
            return ans

    candidates, rejected, _ = extract_claims(
        pdf, Client(), 0, lambda *a: None, packets=[packet], prepared_targets=True,
        extraction_mode='target_context', target_words=200, bibliography={},
        closed_relations=True, reference=ref,
    )
    if expected_reason:
        assert not candidates
        assert rejected == {expected_reason: 1}
    else:
        assert rejected == {}
        assert len(candidates) == 1
        assert candidates[0]['subject_source_mention'] == expected_subject_mention
        assert candidates[0]['extraction_review_status'] == 'not_flagged'
        assert extraction_warnings(candidates[0]) == []


def test_closed_recall_fixes_predicate_normalization_abbreviations_and_process_nouns(tmp_path, ref):
    pdf = tmp_path / 'recall_fixes.pdf'
    _make_pdf(pdf)
    packet = {
        'packet_number': 1,
        'section_heading': 'Results',
        'page_start': 1,
        'page_end': 1,
        'word_count': 65,
        'estimated_token_count': 120,
        'sentences': [
            {'text': 'Inoculation with Bacillus subtilis was tested alongside plant growth-promoting rhizobacteria (PGPR).', 'page': 1},
            {'text': 'Inoculation with B. subtilis increased shoot phosphorus content, and co-inoculation of potato with plant growth-promoting rhizobacteria (PGPR) enhanced plant biomass.', 'page': 1},
            {'text': 'Enhanced expression of PHT1 genes enhanced root phosphorus uptake.', 'page': 1},
            {'text': 'Vacuolar sequestration of surplus phosphorus reduced phosphorus utilization efficiency.', 'page': 1},
            {'text': 'Extra root sugar partitioning determines root growth.', 'page': 1},
            {'text': 'Bare Agent A increased root growth, while Trait Q correlated with Trait R.', 'page': 1},
        ],
    }

    class RecallClient:
        def chat_json(self, system, user, **kwargs):
            return {'claims': [
                {
                    'subject': 'B. subtilis inoculation',
                    'predicate_alias': 'positively regulates characteristic',
                    'object': 'shoot phosphorus content',
                    'qualifiers': {},
                    'subject_anchor': 'Inoculation with B. subtilis',
                    'relation_anchor': 'increased',
                    'object_anchor': 'shoot phosphorus content',
                    'evidence_sentence_ids': [2],
                },
                {
                    'subject': 'co-inoculation with PGPR',
                    'predicate_alias': 'RO:0019001',
                    'object': 'plant biomass',
                    'qualifiers': {},
                    'subject_anchor': 'co-inoculation of potato with plant growth-promoting rhizobacteria',
                    'relation_anchor': 'enhanced',
                    'object_anchor': 'plant biomass',
                    'evidence_sentence_ids': [2],
                },
                {
                    'subject': 'PHT1 gene expression',
                    'predicate_alias': 'P1',
                    'object': 'root phosphorus uptake',
                    'qualifiers': {},
                    'subject_anchor': 'Enhanced expression of PHT1 genes',
                    'relation_anchor': 'enhanced',
                    'object_anchor': 'root phosphorus uptake',
                    'evidence_sentence_ids': [3],
                },
                {
                    'subject': 'vacuolar sequestration of surplus phosphorus',
                    'predicate_alias': 'negatively regulates characteristic',
                    'object': 'phosphorus utilization efficiency',
                    'qualifiers': {},
                    'subject_anchor': 'Vacuolar sequestration of surplus phosphorus',
                    'relation_anchor': 'reduced',
                    'object_anchor': 'phosphorus utilization efficiency',
                    'evidence_sentence_ids': [4],
                },
                {
                    'subject': 'root sugar partitioning',
                    'predicate_alias': 'P4',
                    'object': 'root growth',
                    'qualifiers': {},
                    'subject_anchor': 'root sugar partitioning',
                    'relation_anchor': 'determines',
                    'object_anchor': 'root growth',
                    'evidence_sentence_ids': [5],
                },
                {
                    'subject': 'Agent A',
                    'predicate_alias': 'P1',
                    'object': 'root growth',
                    'qualifiers': {},
                    'subject_anchor': 'Agent A',
                    'relation_anchor': 'increased',
                    'object_anchor': 'root growth',
                    'evidence_sentence_ids': [6],
                },
                {
                    'subject': 'Trait Q',
                    'predicate_alias': 'causes',
                    'object': 'Trait R',
                    'qualifiers': {},
                    'subject_anchor': 'Trait Q',
                    'relation_anchor': 'correlated with',
                    'object_anchor': 'Trait R',
                    'evidence_sentence_ids': [6],
                },
                {
                    'subject': 'invented effect',
                    'predicate_alias': 'P5',
                    'object': 'Trait R',
                    'qualifiers': {},
                    'subject_anchor': 'Trait Q',
                    'relation_anchor': 'correlated with',
                    'object_anchor': 'Trait R',
                    'evidence_sentence_ids': [6],
                },
                {
                    'subject': 'Trait Q',
                    'predicate_alias': 'P99',
                    'object': 'Trait R',
                    'qualifiers': {},
                    'subject_anchor': 'Trait Q',
                    'relation_anchor': 'correlated with',
                    'object_anchor': 'Trait R',
                    'evidence_sentence_ids': [6],
                },
            ]}

    candidates, rejected, _ = extract_claims(
        pdf, RecallClient(), 0, lambda *a: None, packets=[packet],
        extraction_mode='target_context', target_words=200, bibliography={},
        closed_relations=True, reference=ref,
    )
    assert [c['subject'] for c in candidates] == [
        'Bacillus subtilis inoculation',
        'co-inoculation with plant growth-promoting rhizobacteria',
        'PHT1 gene expression',
        'vacuolar sequestration of surplus phosphorus',
        'root sugar partitioning',
    ]
    assert [c['subject_source_mention'] for c in candidates[:2]] == [
        'Inoculation with B. subtilis',
        'co-inoculation of potato with plant growth-promoting rhizobacteria',
    ]
    assert [c['predicate_alias'] for c in candidates] == ['P1', 'P1', 'P1', 'P2', 'P4']
    assert all(extraction_warnings(c) == [] for c in candidates)
    assert all(c['extraction_review_status'] == 'not_flagged' for c in candidates)
    assert rejected == {
        'extraction_quality_rejected': 1,
        'malformed_predicate_alias': 2,
        'unanchored_evidence': 1,
    }


