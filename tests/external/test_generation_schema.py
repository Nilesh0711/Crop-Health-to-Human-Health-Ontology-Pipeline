"""Unsupported grammar keywords stay enforced by the application validator."""
from copy import deepcopy
import pytest

from backend.providers.generation_schema import for_model
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA


def test_decoder_compatibility_does_not_mutate_or_weaken_evidence_validation():
    original = deepcopy(EXTRACTION_RESPONSE_SCHEMA)
    native = for_model(original, 'deepseek-v4.1-flash')
    ids = native['properties']['claims']['items']['properties']['evidence_sentence_ids']
    assert 'uniqueItems' not in ids
    assert original == EXTRACTION_RESPONSE_SCHEMA
    assert original['properties']['claims']['items']['properties']['evidence_sentence_ids']['uniqueItems'] is True
    response = {'claims': [{'subject': 'Material M', 'relation': 'reduces', 'object': 'Trait Q',
                           'qualifiers': {}, 'evidence_sentence_ids': [1, 1]}]}
    with pytest.raises(SchemaValidationError, match='uniqueItems'):
        validate_schema(response, original)


def test_keyword_like_property_names_and_enum_literals_are_not_modified():
    schema = {'type': 'object', 'properties': {
        'uniqueItems': {'type': 'boolean'},
        'value': {'enum': [{'uniqueItems': True}]},
        'nested': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'integer'}}}}
    original = deepcopy(schema)
    native = for_model(schema, 'deepseek-v4.1-flash')
    assert native['properties']['uniqueItems'] == {'type': 'boolean'}
    assert native['properties']['value']['enum'] == [{'uniqueItems': True}]
    assert 'uniqueItems' not in native['properties']['nested']
    assert schema == original
    assert for_model(schema, 'other-model') == original
