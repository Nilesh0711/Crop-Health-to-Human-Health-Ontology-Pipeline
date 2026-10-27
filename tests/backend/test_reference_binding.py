"""A quoted antecedent need not contain the subsequent pronoun itself."""
from copy import deepcopy
from types import SimpleNamespace

from backend.config.settings import Settings
from backend.domain.alignment import grounded_alignment
from backend.stages.source_validation.checker import validate_source_claim


def mapping():
    return dict(canonical='wheat grains', mention='These infected grains', kind='context_reference',
                evidence='Fusarium head blight affects wheat grains.',
                explanation='These infected grains refers to the wheat grains named in the preceding sentence.')


ANTECEDENT = 'Fusarium head blight affects wheat grains.'
EVIDENCE = 'These infected grains may contain mycotoxins.'


def test_both_antecedent_and_reference_are_grounded_without_a_single_impossible_quote():
    assert grounded_alignment(mapping(), 'wheat grains', EVIDENCE, ANTECEDENT + ' ' + EVIDENCE)


def test_missing_antecedent_invented_reference_and_different_canonical_entity_still_fail():
    assert not grounded_alignment(mapping(), 'wheat grains', EVIDENCE, 'An unrelated rice experiment.')
    assert not grounded_alignment(mapping(), 'wheat grains', 'Grains may contain mycotoxins.', ANTECEDENT)
    assert not grounded_alignment(mapping(), 'rice grains', EVIDENCE, ANTECEDENT + ' ' + EVIDENCE)
    bad = {**mapping(), 'evidence':'Fusarium head blight affects rice grains.'}
    assert not grounded_alignment(bad, 'wheat grains', EVIDENCE, ANTECEDENT + ' ' + EVIDENCE)


def test_explicit_definition_still_requires_both_names_in_the_definition_quote():
    assert not grounded_alignment({**mapping(), 'kind':'explicit_definition'},
                                  'wheat grains', EVIDENCE, ANTECEDENT + ' ' + EVIDENCE)


def test_source_validation_preserves_anaphor_condition_and_conditional_edge():
    candidate = dict(candidate_id='reference', subject='wheat grains', relation='has_contaminant',
        object='mycotoxins', subject_type='Crop', object_type='Contaminant', certainty='conditional',
        source_evidence_span=EVIDENCE, source_evidence_sentence_numbers=[2], subject_source_mention='These infected grains',
        object_source_mention='mycotoxins', relation_trigger='contain', source_local_context=ANTECEDENT + ' ' + EVIDENCE,
        qualifiers={'condition':'Fusarium head blight infection'}, claim_text_origin='extracted',
        claim_text='Wheat grains affected by Fusarium head blight may contain mycotoxins.')
    answer = dict(decision='entailed', relation_match=True, direction_match=True, certainty_match=True,
        claim_text_match=True, occurrence_provenance_fields=[],
        entity_alignment=dict(subject=mapping(), object=dict(canonical='mycotoxins', mention='mycotoxins',
            kind='same_concept', evidence=EVIDENCE, explanation='The exact contaminant is named.')),
        proof=dict(subject='These infected grains', relation='contain', object='mycotoxins'),
        rationale='The original two sentences explicitly bind the grain reference and the conditional contamination edge.')
    client = SimpleNamespace(settings=Settings(), chat_json=lambda *args,**kwargs:deepcopy(answer))
    result = validate_source_claim(candidate, client)
    assert result['decision'] == 'entailed'
    assert candidate['qualifiers']['condition'] == 'Fusarium head blight infection'
    assert candidate['certainty'] == 'conditional'
