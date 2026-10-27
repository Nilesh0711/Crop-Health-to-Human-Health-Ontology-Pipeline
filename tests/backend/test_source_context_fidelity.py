"""Reference bindings are model judgements; source IDs/quotes stay verifiable."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient
from backend.stages.source_validation.checker import validate_source_claim
from backend.stages.source_validation.context import section_context
from tests.backend.test_source_abbreviations import Client, fixture


def grain_example():
    candidate, answer = fixture()
    heading = 'Fusarium head blight of wheat, Great Plains of North America'
    span = ('Mycotoxin contamination of the grain has no effect on yield, but on the uses of the grain: '
            'this represents a loss of grain for food and a substantial economic loss for growers.')
    candidate.update(subject='Fusarium head blight-affected wheat grain', object='mycotoxins',
        relation='has_contaminant', certainty='descriptive', claim_text=span,
        qualifiers={'crop_or_species':'wheat', 'condition':'Fusarium head blight'},
        subject_source_mention='the grain', object_source_mention='Mycotoxin contamination',
        relation_trigger='Mycotoxin contamination', source_evidence_span=span,
        source_local_context='[LOCAL SECTION HEADING] '+heading+'\nThe disease reduces yields. '+span)
    answer.update(proof={'subject':'the grain','relation':'Mycotoxin contamination of','object':'Mycotoxin contamination'},
        abbreviation_review={'status':'clear','corrections':[],'unresolved':[]},
        entity_alignment={
            'subject': {'canonical':candidate['subject'], 'mention':'the grain','kind':'context_reference',
                'evidence':heading,'explanation':'The supplied heading and grain sentence identify affected wheat grain, not the disease itself.'},
            'object': {'canonical':'mycotoxins','mention':'Mycotoxin contamination','kind':'same_concept',
                'evidence':'Mycotoxin contamination of the grain','explanation':'This is contaminant occurrence, with grain use distinguished from yield.'}})
    return candidate, answer


def test_contaminated_grain_has_contextual_identity_without_full_canonical_phrase_in_heading():
    claim, answer = grain_example(); before = deepcopy(claim)
    assert validate_source_claim(claim, Client(answer))['decision'] == 'entailed'
    assert claim == before
    assert 'no effect on yield' in claim['source_evidence_span']


@pytest.mark.parametrize('change', ['invented_context','invented_mention','wrong_canonical','different','model_review','false_direction','false_claim_text'])
def test_real_quotes_and_explicit_model_agreement_still_required(change):
    claim, answer = grain_example()
    if change=='invented_context': answer['entity_alignment']['subject']['evidence']='An invented source definition'
    elif change=='invented_mention': answer['entity_alignment']['subject']['mention']='barley grain'
    elif change=='wrong_canonical': answer['entity_alignment']['subject']['canonical']='rice grain'
    elif change=='different': answer['entity_alignment']['subject']['kind']='different'
    elif change=='model_review': answer['decision']='needs_review'
    elif change=='false_direction': answer['direction_match']=False
    else: answer['claim_text_match']=False
    assert validate_source_claim(claim,Client(answer))['decision'] != 'entailed'


def test_noop_name_annotation_does_not_reject_an_already_spelled_out_name():
    claim, answer=grain_example()
    answer['abbreviation_review']={'status':'expanded','unresolved':[], 'corrections':[{
        'field':'subject','before':claim['subject'],'after':claim['subject'],'short_form':'FHB',
        'full_form':'Fusarium head blight','definition_quote':'FHB', 'rationale':'Canonical name already expanded.'}]}
    result=validate_source_claim(claim,Client(answer))
    assert result['decision']=='entailed' and result['unchanged_name_annotations']
    assert 'applied_name_corrections' not in result and 'source_name_corrections' not in claim


def test_already_applied_expansion_is_idempotent_but_an_actual_undefined_abbreviation_is_not():
    claim, answer=fixture()
    claim['subject']='Fusarium head blight'
    answer['abbreviation_review']['corrections']=answer['abbreviation_review']['corrections'][:1]
    claim['claim_text']='Fusarium head blight reduces grain quality.'
    claim['qualifiers']={'condition':'Fusarium head blight infection'}
    result=validate_source_claim(claim,Client(answer))
    assert result['decision']=='entailed' and 'applied_name_corrections' not in result
    claim, answer=fixture()
    answer['abbreviation_review']['corrections'][0].update(after='FHB')
    assert validate_source_claim(claim,Client(answer))['decision']=='needs_review'


def test_same_section_context_retains_distant_antecedent_but_stops_at_section_and_chapter_boundaries():
    packet={'sentences':[{'text':'Different disease.','section_heading':'Earlier','chapter_id':'one','page':1}]
        +[{'text':text,'section_heading':'Local','chapter_id':'one','page':2} for text in
          ['Full disease name (FDN) was defined.', 'Other methods.', 'More methods.', 'FDN reduces grain quality.']]
        +[{'text':'Unrelated result.','section_heading':'Local','chapter_id':'two','page':3}]}
    claim={'source_evidence_sentence_numbers':[5], 'source_evidence_section_heading':'Local'}
    context=section_context(claim,packet)
    assert 'Full disease name (FDN)' in context and '[5, PDF page 2]' in context
    assert 'Different disease' not in context and 'Unrelated result' not in context


def test_bad_source_proof_regenerates_from_original_input_without_weakening_quotes():
    claim, good=grain_example(); invalid=deepcopy(good)
    invalid['proof']['object']='mycotoxins not actually quoted'
    client=OpenCodeClient(Settings(),task='source')
    replies=[{'finish_reason':'stop','content':json.dumps(a)} for a in (invalid,good)]
    with patch.object(client._inference,'chat',side_effect=replies) as call:
        result=validate_source_claim(claim,client)
    assert result['decision']=='entailed' and call.call_count==2
    assert call.call_args_list[0].args[2]==call.call_args_list[1].args[2]
    assert all(c.args[0]==Settings().source_model for c in call.call_args_list)


def test_quote_from_the_wrong_endpoint_cannot_fill_a_proof_slot():
    claim, answer=grain_example()
    answer['proof']['object']='the grain'
    result=validate_source_claim(claim,Client(answer))
    assert result['decision']=='needs_review'
    assert any('inconsistent endpoint' in reason for reason in result['reasons'])


def test_same_section_context_is_available_to_the_model_without_becoming_selected_effect_proof():
    claim, answer = grain_example()
    claim['source_section_context'] = '[1, PDF page 2] Fusarium head blight affects wheat.\n[2, PDF page 2] A distant effect.'
    class InspectingClient(Client):
        def chat_json(self, system, user, **kwargs):
            payload = json.loads(user)
            assert payload['nearby_context'] == claim['source_section_context']
            assert payload['selected_evidence_span'] == claim['source_evidence_span']
            return super().chat_json(system, user, **kwargs)
    answer['entity_alignment']['subject']['evidence'] = 'Fusarium head blight affects wheat.'
    assert validate_source_claim(claim, InspectingClient(answer))['decision'] == 'entailed'
    answer['proof']['relation'] = 'A distant effect'
    assert validate_source_claim(claim, InspectingClient(answer))['decision'] == 'needs_review'
