"""Explicit binding context is separate from findings and optional for non-decisions."""
from copy import deepcopy

import pytest

from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.stages.external_validation.paper_proofs import PAPER_DECISION_SCHEMA, guard_paper
from backend.stages.external_validation.synthesis import judge_paper
from tests.external.test_paper_synthesis import Client, configuration, example


def defined_name_example():
    claim, document, answer = example()
    definition = 'Fusarium head blight (FHB) is a wheat disease.'
    answer['decision']['entity_alignment']['subject'].update(mention='Fusarium head blight (FHB)', evidence=definition)
    return claim, document, answer['decision']


def test_definition_mention_can_differ_from_the_literal_abbreviation_in_the_finding():
    claim, document, row = defined_name_example()
    result = guard_paper(claim, document, row)
    assert result['decision'] == 'supported' and result['alignment_context_roles'] == ['subject']
    assert result['evidence'][0]['subject_mention'] == 'FHB'
    assert result['evidence'][0]['sentence_numbers'] == [2]
    assert result['binding_context'][0]['sentence_numbers'] == [1]


def test_rechecking_cached_definition_alignment_does_not_duplicate_context_roles():
    claim, document, row = defined_name_example()
    first = guard_paper(claim, document, row)
    assert guard_paper(claim, document, first) == first


@pytest.mark.parametrize('decision', ['supported', 'insufficient'])
def test_paper_request_accepts_context_contract_without_unnecessary_regeneration(decision):
    claim, document, answer = example()
    if decision == 'supported':
        _, _, answer['decision'] = defined_name_example()
    else:
        answer['decision'].update(decision=decision, evidence=[])
        del answer['decision']['context_selections']
    client = Client({'paper-a': answer}, None)
    result, original = judge_paper(client, claim, document, configuration())
    assert len(client.calls) == 1 and result['decision'] == decision
    assert original == answer


@pytest.mark.parametrize('problem', ['uncited_definition', 'unknown_paper', 'invented_mention', 'no_identity_binding', 'missing_finding_endpoint'])
def test_context_cannot_invent_or_replace_finding_proof(problem):
    claim, document, row = defined_name_example()
    if problem == 'uncited_definition': row['context_selections'] = []
    elif problem == 'unknown_paper': row['context_selections'][0]['chunk_id'] = 'other-paper:0'
    elif problem == 'invented_mention': row['entity_alignment']['subject']['mention'] = 'An invented disease'
    elif problem == 'no_identity_binding': row['entity_alignment']['subject']['kind'] = 'same_concept'
    else: row['evidence'][0]['subject_mention'] = row['entity_alignment']['subject']['mention']
    result = guard_paper(claim, document, row)
    assert result['decision'] == 'needs_review' and result['evidence'] == []


@pytest.mark.parametrize('decision', ['insufficient', 'needs_review'])
def test_nondecisive_response_can_omit_unused_context_without_inventing_an_empty_model_field(decision):
    claim, document, answer = example()
    row = answer['decision']; row.update(decision=decision, evidence=[])
    del row['context_selections']
    original = deepcopy(row)
    validate_schema(row, PAPER_DECISION_SCHEMA)
    result = guard_paper(claim, document, row)
    assert result['decision'] == decision and result['binding_context'] == []
    assert result['context_selection_status'] == 'not_supplied; no additional context admitted'
    assert 'context_selections' not in result and row == original


@pytest.mark.parametrize('decision', ['supported', 'contradicted'])
def test_decisive_response_still_requires_explicit_context_selection_field(decision):
    _, _, answer = example()
    row = answer['decision']; row['decision'] = decision
    del row['context_selections']
    with pytest.raises(SchemaValidationError):
        validate_schema(row, PAPER_DECISION_SCHEMA)


@pytest.mark.parametrize('missing', ['matches', 'entity_alignment', 'qualifier_alignment', 'evidence', 'rationale'])
def test_nondecisive_response_still_requires_core_assessment_fields(missing):
    _, _, answer = example()
    row = answer['decision']; row['decision'] = 'insufficient'
    del row[missing]
    with pytest.raises(SchemaValidationError):
        validate_schema(row, PAPER_DECISION_SCHEMA)


def test_nondecisive_context_is_validated_when_it_is_supplied():
    claim, document, answer = example()
    row = answer['decision']; row.update(decision='insufficient', evidence=[])
    row['context_selections'][0]['sentence_numbers'] = [999]
    result = guard_paper(claim, document, row)
    assert result['decision'] == 'needs_review' and result['binding_context'] == []
