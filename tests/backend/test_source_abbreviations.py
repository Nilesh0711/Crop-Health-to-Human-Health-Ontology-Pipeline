"""Source-model expansions stay grounded, atomic, auditable and batch-safe."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.shared.batching import iter_batched_judgments
from backend.stages.source_validation.checker import validate_source_claim


def fixture():
    definition = 'Fusarium head blight (FHB)'
    span = 'FHB reduces grain quality.'
    claim = dict(candidate_id='one', subject='FHB', relation='reduces', object='grain quality',
        subject_type='PlantDisease', object_type='PlantTrait', claim_text=span, claim_text_origin='extracted',
        subject_source_mention='FHB', object_source_mention='grain quality', relation_trigger='reduces',
        certainty='causal', qualifiers={'condition': 'FHB infection'}, source_evidence_sentence_numbers=[2],
        source_evidence_span=span, source_local_context=definition + ' is a disease of wheat. ' + span,
        source_context_packet=definition + ' is a disease of wheat. ' + span)
    changes = [dict(field=field, before=before, after=before.replace('FHB', 'Fusarium head blight'),
        short_form='FHB', full_form='Fusarium head blight', definition_quote=definition,
        rationale='The source explicitly defines the disease abbreviation.')
        for field, before in [('subject', 'FHB'), ('claim_text', span), ('qualifiers.condition', 'FHB infection')]]
    answer = dict(decision='entailed', relation_match=True, direction_match=True, certainty_match=True,
        claim_text_match=True, occurrence_provenance_fields=[], rationale='The expanded claim preserves the source assertion.',
        proof={'subject': 'FHB', 'relation': 'reduces', 'object': 'grain quality'},
        entity_alignment={
            'subject': {'canonical': 'Fusarium head blight', 'mention': 'FHB', 'kind': 'explicit_definition',
                        'evidence': definition, 'explanation': 'The supplied definition identifies the disease.'},
            'object': {'canonical': 'grain quality', 'mention': 'grain quality', 'kind': 'same_concept',
                       'evidence': 'grain quality', 'explanation': 'Exact endpoint.'}},
        abbreviation_review={'status': 'expanded', 'corrections': changes, 'unresolved': []})
    return claim, answer


class Client:
    settings = SimpleNamespace(model='test-source')
    def __init__(self, answer): self.answer, self.calls = answer, 0
    def chat_json(self, system, user, **kwargs):
        self.calls += 1
        self.system, self.payload, self.schema = system, json.loads(user), kwargs['json_schema']
        return deepcopy(self.answer)


def test_full_names_corrected_in_same_source_call_with_original_proof_retained():
    claim, answer = fixture()
    before = deepcopy(claim)
    client = Client(answer)
    result = validate_source_claim(claim, client)
    assert result['decision'] == 'entailed'
    assert claim['subject'] == 'Fusarium head blight'
    assert claim['claim_text'] == 'Fusarium head blight reduces grain quality.'
    assert claim['qualifiers']['condition'] == 'Fusarium head blight infection'
    for key in ('candidate_id', 'source_evidence_span', 'source_evidence_sentence_numbers', 'subject_source_mention', 'object_source_mention', 'relation_trigger'):
        assert claim[key] == before[key]
    assert len(result['applied_name_corrections']) == 3
    assert len(claim['source_name_corrections']) == 3
    assert client.calls == 1
    assert 'abbreviation_review' in client.schema['required']


@pytest.mark.parametrize('problem', ['invented_definition', 'other_scientific_edit', 'wrong_before', 'unresolved', 'unsupported'])
def test_untrusted_or_unverified_expansion_cannot_modify_claim(problem):
    claim, answer = fixture()
    if problem == 'invented_definition':
        answer['abbreviation_review']['corrections'][0]['definition_quote'] = 'Fusarium blight disorder (FHB)'
    elif problem == 'other_scientific_edit':
        answer['abbreviation_review']['corrections'][1]['after'] = 'Fusarium head blight increases grain quality.'
    elif problem == 'wrong_before':
        answer['abbreviation_review']['corrections'][0]['before'] = 'A different disease'
    elif problem == 'unresolved':
        answer['abbreviation_review'] = {'status': 'unresolved', 'corrections': [], 'unresolved': ['FHB']}
    else:
        answer['decision'] = 'unsupported'
    before = deepcopy(claim)
    result = validate_source_claim(claim, Client(answer))
    assert result['decision'] != 'entailed'
    assert claim == before


def test_batch_applies_expansions_only_to_its_own_candidate_without_extra_calls():
    claim, answer = fixture()
    other, second = fixture()
    other['candidate_id'] = 'two'
    first = {**answer, 'candidate_id': 'one'}
    second['candidate_id'] = 'two'
    client = Client({'results': [second, first]})
    results = list(iter_batched_judgments([claim, other], client, validate_source_claim, 3, label='Source'))
    assert all(row['decision'] == 'entailed' for _, row in results)
    assert claim['subject'] == other['subject'] == 'Fusarium head blight'
    assert claim['source_name_corrections'] is not other['source_name_corrections']
    assert client.calls == 1
