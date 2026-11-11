"""Five-field extraction retains source context and restrictions without a summary."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.providers.response_repair import validate_schema
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA, EXTRACTION_CONTRACT_VERSION
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.ontology_grounding.grounder import request_packet
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from tests.backend.test_extraction_targets import claim, extract_fixture, row, packet


def test_provisional_types_are_preserved_as_hints_without_a_type_whitelist(tmp_path):
    class Client:
        def chat_json(self, system, user, **kwargs):
            item = claim([2]); item.update(subject_type='grain material', object_type='chemical entity')
            value = {'claims': [item]}; kwargs['response_check'](value)
            return value
    result, rejected, _ = extract_fixture(tmp_path, Client(), mode='whole_packet')
    assert not rejected and len(result) == 1
    item = result[0]
    assert item['schema_error'] is None and item['extraction_contract_version'] == EXTRACTION_CONTRACT_VERSION == 11
    assert item['extraction_type_hints'] == {'subject': 'grain material', 'object': 'chemical entity'}
    assert 'subject_type' not in item and 'object_type' not in item and 'claim_text' not in item
    payload, _ = request_packet(result, ConceptIndex(Catalog(RESOURCE_DIR)))
    assert payload['claims']['C1']['extraction_type_hints'] == item['extraction_type_hints']


def test_long_conditions_and_numbers_survive_without_truncation(tmp_path):
    condition = 'Condition described by the source: ' + 'detailed restriction; ' * 15
    class Client:
        def chat_json(self, *args, **kwargs):
            item = claim([2]); item['qualifiers'].update(condition=condition, metric='15% lower; 20 mg/L; seven days')
            value = {'claims': [item]}; kwargs['response_check'](value)
            return value
    result, rejected, _ = extract_fixture(tmp_path, Client(), mode='whole_packet')
    assert not rejected
    assert result[0]['qualifiers']['condition'] == condition.strip()
    assert result[0]['qualifiers']['metric'] == '15% lower; 20 mg/L; seven days'


def test_grammatically_separated_negation_is_not_lost_or_assigned_an_invented_quote(tmp_path):
    sentence = 'We did not observe that Treatment T changed root growth.'
    item = {'subject': 'Treatment T', 'relation': 'did not change', 'object': 'root growth',
            'qualifiers': {}, 'evidence_sentence_ids': [1]}
    class Client:
        def chat_json(self, *args, **kwargs):
            value = {'claims': [item]}; kwargs['response_check'](value)
            return value
    pdf = tmp_path / 'source.pdf'; pdf.write_bytes(b'fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Synthetic fixture'):
        result, rejected, _ = extract_claims(pdf, Client(), 0, lambda *args: None,
            packets=[packet([row(1, text=sentence)])], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert not rejected and result[0]['relation'] == 'did not change'
    assert result[0]['relation_trigger'] == ''
    assert 'review_nonliteral_relation_wording' in result[0]['extraction_review_hints']
    assert result[0]['source_evidence_span'] == sentence


def test_grounding_receives_exact_shared_reading_context_without_claim_text():
    context = '[1] Compound T was applied.\n[2] This treatment may reduce Process P.'
    first = {'candidate_id': 'a', 'subject': 'This treatment', 'relation': 'may reduce', 'object': 'Process P',
             'qualifiers': {}, 'source_evidence_span': 'This treatment may reduce Process P.',
             'source_reading_context': context, 'source_document_id': 'doc-a'}
    second = {**deepcopy(first), 'candidate_id': 'b', 'claim_text': 'Do not send this obsolete generated summary.'}
    payload, _ = request_packet([first, second], ConceptIndex(Catalog(RESOURCE_DIR)))
    assert len(payload['reading_contexts']) == 1
    assert list(payload['reading_contexts'].values()) == [context]
    assert payload['claims']['C1']['reading_context_id'] == payload['claims']['C2']['reading_context_id']
    assert 'claim_text' not in payload['claims']['C1'] and 'claim_text' not in payload['claims']['C2']


def test_more_than_three_evidence_sentences_preserve_all_fragments(tmp_path):
    item = {'subject': 'Treatment T', 'relation': 'may reduce', 'object': 'root growth', 'qualifiers': {},
            'evidence_sentence_ids': [1, 2, 3, 4]}
    validate_schema({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA)
    class Client:
        def chat_json(self, *args, **kwargs):
            value = {'claims': [item]}; kwargs['response_check'](value)
            return value
    pdf = Path(tmp_path) / 'source.pdf'; pdf.write_bytes(b'fixture')
    sentences = [row(1, text='Treatment T was applied.'), row(2, text='Root growth was measured.'),
                 row(3, text='Conditions remained unchanged.'), row(4, text='It may reduce root growth.')]
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Synthetic fixture'):
        result, rejected, _ = extract_claims(pdf, Client(), 0, lambda *args: None,
            packets=[packet(sentences)], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert not rejected and result[0]['source_evidence_sentence_numbers'] == [1, 2, 3, 4]
    assert len(result[0]['source_evidence_fragments']) == 4
