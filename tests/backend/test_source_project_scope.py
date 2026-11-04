"""Generic scope contracts and routing; these mocks do not benchmark model accuracy."""
from copy import deepcopy
import json

import pytest

from backend.providers.structured_output import parse_json_object, SchemaValidationError
from backend.providers.response_repair import validate_schema
from backend.shared.artifacts import save_rows
from backend.stages.graph_construction.graph import graph_from_candidates
from backend.stages.pathway_assembly.corpus import collect_corpus
from backend.stages.source_validation.checker import validate_source_claim, _SOURCE_DECISION_SCHEMA
from backend.stages.source_validation.review import apply_manual_source_review, source_review_state
from backend.stages.source_validation.scope import (
    PROJECT_SCOPE_INSTRUCTIONS, source_pathway_eligible, source_scope_counts,
)


def example(subject='Factor A', relation='changes', outcome='Process B'):
    text = f'{subject} {relation} {outcome}.'
    candidate = dict(candidate_id='claim-a', subject=subject, relation=relation, object=outcome,
        subject_type='Unresolved', object_type='Unresolved', certainty='descriptive', qualifiers={},
        semantic_validation={'decision': 'not_run'}, claim_text=text, claim_text_origin='extracted',
        subject_source_mention=subject, object_source_mention=outcome, relation_trigger=relation,
        source_evidence_span=text, source_evidence_sentence_numbers=[1])
    answer = dict(decision='entailed', relation_match=True, direction_match=True, certainty_match=True,
        claim_text_match=True, occurrence_provenance_fields=[], rationale='The selected source states this relationship.',
        project_scope={'decision': 'in_scope', 'rationale': 'An upstream biological relationship in the crop system.'},
        abbreviation_review={'status': 'clear', 'corrections': [], 'unresolved': []},
        proof={'subject': subject, 'relation': relation, 'object': outcome},
        entity_alignment={role: {'canonical': name, 'mention': name, 'kind': 'same_concept',
            'evidence': name, 'explanation': 'The literal source endpoint.'}
            for role, name in [('subject', subject), ('object', outcome)]})
    return candidate, answer


class Client:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def chat_json(self, system, user, **kwargs):
        self.calls.append((system, json.loads(user), kwargs))
        parsed, _ = parse_json_object(json.dumps(self.answer))
        validate_schema(parsed, kwargs['json_schema'])
        return parsed


@pytest.mark.parametrize('scope', ['in_scope', 'out_of_scope', 'needs_review'])
def test_scope_is_model_decided_without_changing_fidelity_or_claim(scope):
    candidate, answer = example()
    answer['project_scope'] = {'decision': scope, 'rationale': 'Context-based model scope assessment.'}
    original = deepcopy(candidate)
    client = Client(answer)
    result = validate_source_claim(candidate, client)
    assert result['decision'] == 'entailed'
    assert result['project_scope'] == answer['project_scope']
    assert candidate == original and len(client.calls) == 1
    candidate['source_validation'] = result
    assert source_pathway_eligible(candidate) is (scope == 'in_scope')
    system, payload, _ = client.calls[0]
    assert PROJECT_SCOPE_INSTRUCTIONS in system
    assert payload['claim_hypothesis']['relation'] == original['relation']
    assert payload['selected_evidence_span'] == original['source_evidence_span']


def test_pure_financial_claim_can_be_pdf_supported_but_excluded_from_assembly(tmp_path):
    candidate, answer = example('Market M', 'increases', 'revenue R')
    answer['project_scope'] = {'decision': 'out_of_scope',
                              'rationale': 'This assertion concerns monetary revenue, with no biological or food-quality mechanism.'}
    candidate['source_validation'] = validate_source_claim(candidate, Client(answer))
    save_rows(tmp_path / 'Document A' / 'candidates.jsonl', [candidate])
    assert collect_corpus(tmp_path, provisional=True) == ([], [])
    assert graph_from_candidates([candidate], view='source')['edges'][0]['status'] == 'out_of_scope'
    assert graph_from_candidates([candidate], view='audit')['edges'][0]['project_scope'] == answer['project_scope']
    assert source_scope_counts([candidate]) == {
        'source_project_scope': {'in_scope': 0, 'out_of_scope': 1, 'needs_review': 0, 'not_assessed': 0},
        'pathway_eligible_claims': 0}


def test_upstream_numerical_food_property_can_be_in_scope_without_health_endpoint(tmp_path):
    candidate, answer = example('Treatment T', 'changes', 'food-material property P')
    candidate['qualifiers'] = {'condition': 'Condition K', 'value': '5 units'}
    candidate['source_local_context'] = 'The measured food-material property P changed by 5 units under Condition K.'
    candidate['source_validation'] = validate_source_claim(candidate, Client(answer))
    save_rows(tmp_path / 'Document A' / 'candidates.jsonl', [candidate])
    edges, endpoints = collect_corpus(tmp_path, provisional=True)
    assert len(edges) == 1 and len(endpoints) == 2
    assert edges[0]['qualifiers'] == candidate['qualifiers']
    assert 'need not mention a human-health' in PROJECT_SCOPE_INSTRUCTIONS
    assert 'Numerical values alone neither establish' in PROJECT_SCOPE_INSTRUCTIONS


@pytest.mark.parametrize('scope', [None, 'needs_review', 'out_of_scope'])
def test_unassessed_or_excluded_scope_is_never_assumed_eligible(tmp_path, scope):
    candidate, _ = example()
    candidate['source_validation'] = {'decision': 'entailed'}
    if scope:
        candidate['source_validation']['project_scope'] = {'decision': scope, 'rationale': 'Scope is not confirmed.'}
    save_rows(tmp_path / 'Document A' / 'candidates.jsonl', [candidate])
    assert collect_corpus(tmp_path, provisional=True) == ([], [])
    assert not source_pathway_eligible(candidate)


def test_scope_acceptance_cannot_override_direction_or_source_failure():
    candidate, answer = example()
    answer['direction_match'] = False
    candidate['source_validation'] = validate_source_claim(candidate, Client(answer))
    assert candidate['source_validation']['decision'] == 'needs_review'
    assert candidate['source_validation']['project_scope']['decision'] == 'in_scope'
    assert not source_pathway_eligible(candidate)


def test_scope_is_required_in_live_response_schema():
    _, answer = example()
    answer.pop('project_scope')
    with pytest.raises(SchemaValidationError, match='project_scope'):
        validate_schema(answer, _SOURCE_DECISION_SCHEMA)


def test_manual_scope_review_retains_the_model_reading_and_never_rewrites_claim():
    candidate, answer = example()
    candidate['source_validation'] = {**deepcopy(answer), 'project_scope': {
        'decision': 'needs_review', 'rationale': 'The crop-system context needs a second reading.'}}
    original = deepcopy(candidate)
    apply_manual_source_review(candidate, 'project_scope', 'accept', expected_revision=source_review_state(candidate)['revision'])
    assert candidate['source_validation']['project_scope']['decision'] == 'needs_review'
    assert source_review_state(candidate)['scope_decision'] == 'in_scope'
    candidate['source_validation'].pop('manual_reviews')
    assert candidate == original
