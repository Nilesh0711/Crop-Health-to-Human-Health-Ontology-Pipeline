"""Remove repeated input values, retaining scientific decisions and exact proof."""
from copy import deepcopy

from backend.domain.alignment import ENTITY_ALIGNMENTS_SCHEMA
from backend.stages.source_validation.checker import validate_source_claim, _SOURCE_DECISION_SCHEMA
from backend.stages.source_validation.output import bind_source_output
from tests.backend.test_source_project_scope import example, Client


def compact(answer):
    answer = deepcopy(answer)
    for role in ('subject', 'object'):
        answer['proof'].pop(role, None)
        answer['entity_alignment'][role].pop('canonical', None)
    for edit in answer['abbreviation_review']['corrections']:
        edit.pop('before', None)
    return answer


def test_compact_source_output_produces_the_same_stored_decisions_and_proof():
    candidate, answer = example()
    original = deepcopy(candidate)
    full = validate_source_claim(candidate, Client(answer))
    reduced = validate_source_claim(candidate, Client(compact(answer)))
    assert full == reduced
    assert candidate == original
    assert reduced['proof'] == answer['proof']
    assert reduced['entity_alignment'] == answer['entity_alignment']
    assert reduced['project_scope'] == answer['project_scope']


def test_missing_redundant_fields_are_optional_without_mutating_shared_alignment_schema():
    for role in ('subject', 'object'):
        assert 'canonical' in ENTITY_ALIGNMENTS_SCHEMA['properties'][role]['required']
        assert 'canonical' not in _SOURCE_DECISION_SCHEMA['properties']['entity_alignment']['properties'][role]['required']
    assert _SOURCE_DECISION_SCHEMA['properties']['proof']['required'] == ['relation']


def test_compaction_does_not_overwrite_a_wrong_supplied_value_or_approve_a_false_match():
    candidate, answer = example()
    answer['entity_alignment']['subject']['canonical'] = 'A different entity'
    assert validate_source_claim(candidate, Client(answer))['decision'] == 'needs_review'
    candidate, answer = example()
    answer = compact(answer)
    answer['direction_match'] = False
    assert validate_source_claim(candidate, Client(answer))['decision'] == 'needs_review'
    answer['direction_match'] = True
    answer['entity_alignment']['subject']['mention'] = 'invented source wording'
    result = validate_source_claim(candidate, Client(answer))
    assert result['decision'] == 'needs_review'
    assert result['proof']['subject'] == 'invented source wording'  # Preserved for audit, never repaired into real evidence.


def test_model_proposed_name_expansions_keep_original_before_values_and_canonical_binding():
    candidate, answer = example(subject='DD')
    definition = 'Disease D (DD)'
    candidate['source_local_context'] = definition + ' changes Process B.'
    candidate['claim_text'] = 'DD changes Process B.'
    edits = [{'field': field, 'before': before, 'after': before.replace('DD', 'Disease D'),
              'short_form': 'DD', 'full_form': 'Disease D', 'definition_quote': definition,
              'rationale': 'The supplied source defines the name.'}
             for field, before in [('subject', candidate['subject']), ('claim_text', candidate['claim_text'])]]
    answer['abbreviation_review'] = {'status': 'expanded', 'corrections': edits, 'unresolved': []}
    answer['entity_alignment']['subject'].update(canonical='Disease D', kind='explicit_definition', evidence=definition)
    full_candidate, reduced_candidate = deepcopy(candidate), deepcopy(candidate)
    full = validate_source_claim(full_candidate, Client(answer))
    reduced = validate_source_claim(reduced_candidate, Client(compact(answer)))
    assert full['decision'] == reduced['decision'] == 'entailed'
    assert full == reduced and full_candidate == reduced_candidate
    assert reduced_candidate['subject'] == 'Disease D'
    assert reduced['applied_name_corrections'][0]['before'] == 'DD'


def test_sequential_before_values_follow_only_model_proposed_edits_without_guessing_expansions():
    candidate, answer = example(subject='AA and BB')
    answer['abbreviation_review']['corrections'] = [
        {'field': 'subject', 'after': 'Factor A and BB'},
        {'field': 'subject', 'after': 'Factor A and Factor B'}]
    original = deepcopy(answer)
    bound = bind_source_output(candidate, answer)
    assert [edit['before'] for edit in bound['abbreviation_review']['corrections']] == ['AA and BB', 'Factor A and BB']
    assert answer == original and candidate['subject'] == 'AA and BB'
