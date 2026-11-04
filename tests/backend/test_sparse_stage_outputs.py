"""Sparse wire responses retain the scientific information and stored contracts."""
from copy import deepcopy

import pytest

from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import _proposed_candidate
from backend.stages.source_validation.checker import _SOURCE_DECISION_SCHEMA, validate_source_claim
from backend.stages.source_validation.output import bind_source_output
from tests.backend.test_compact_source_output import compact
from tests.backend.test_source_project_scope import example, Client
from tests.backend.test_extraction_targets import claim


def test_sparse_extraction_preserves_claim_text_and_nonempty_qualifiers():
    full = claim([1])
    reduced = deepcopy(full)
    reduced.pop('source_context', None)
    reduced['qualifiers'] = {key: value for key, value in full['qualifiers'].items() if value}
    validate_schema({'claims': [reduced]}, EXTRACTION_RESPONSE_SCHEMA)
    assert _proposed_candidate(full) == _proposed_candidate(reduced)
    assert reduced['claim_text'] == full['claim_text']
    reduced.pop('claim_text')
    with pytest.raises(SchemaValidationError, match='claim_text'):
        validate_schema({'claims': [reduced]}, EXTRACTION_RESPONSE_SCHEMA)


def test_empty_qualifiers_are_valid_but_unknown_qualifiers_are_not_discarded():
    value = claim([1]); value['qualifiers'] = {}
    validate_schema({'claims': [value]}, EXTRACTION_RESPONSE_SCHEMA)
    value['qualifiers']['invented_condition_key'] = 'A value'
    with pytest.raises(SchemaValidationError, match='additionalProperties'):
        validate_schema({'claims': [value]}, EXTRACTION_RESPONSE_SCHEMA)


def test_sparse_source_response_restores_only_known_duplicates_and_empty_containers():
    candidate, answer = example()
    for role in ('subject', 'object'):
        answer['entity_alignment'][role]['evidence'] = answer['entity_alignment'][role]['mention']
    full = validate_source_claim(deepcopy(candidate), Client(answer))
    reduced = compact(answer)
    reduced.pop('occurrence_provenance_fields')
    reduced['abbreviation_review'] = {'status': 'clear'}
    for role in ('subject', 'object'):
        reduced['entity_alignment'][role].pop('evidence')
    validate_schema(reduced, _SOURCE_DECISION_SCHEMA)
    original = deepcopy(reduced)
    assert validate_source_claim(deepcopy(candidate), Client(reduced)) == full
    assert reduced == original


@pytest.mark.parametrize('kind', ['context_reference', 'explicit_definition', 'coordination', 'different'])
def test_binding_evidence_cannot_be_omitted_for_other_alignment_kinds(kind):
    _, answer = example(); answer = compact(answer)
    answer['entity_alignment']['subject'].update(kind=kind)
    answer['entity_alignment']['subject'].pop('evidence')
    with pytest.raises(SchemaValidationError, match='evidence'):
        validate_schema(answer, _SOURCE_DECISION_SCHEMA)


@pytest.mark.parametrize('review', [{'status': 'expanded'}, {'status': 'expanded', 'corrections': []},
                                  {'status': 'unresolved'}, {'status': 'unresolved', 'unresolved': []}, {}])
def test_abbreviation_omissions_never_mean_checked_or_resolved(review):
    _, answer = example(); answer = compact(answer)
    answer['abbreviation_review'] = review
    with pytest.raises(SchemaValidationError):
        validate_schema(answer, _SOURCE_DECISION_SCHEMA)


def test_sparse_binding_does_not_replace_explicit_wrong_evidence_or_scientific_flags():
    candidate, answer = example(); answer = compact(answer)
    answer['entity_alignment']['subject']['evidence'] = 'Invented binding quote'
    bound = bind_source_output(candidate, answer)
    assert bound['entity_alignment']['subject']['evidence'] == 'Invented binding quote'
    assert validate_source_claim(candidate, Client(answer))['decision'] == 'needs_review'
    candidate, answer = example(); answer['claim_text_match'] = False
    assert validate_source_claim(candidate, Client(compact(answer)))['decision'] == 'needs_review'
