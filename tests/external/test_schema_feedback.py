"""Structural diagnostics remain actionable without enforcing semantic type pairs."""
from copy import deepcopy
from dataclasses import replace
import json
import pytest
from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA


def claim(relation='has_trait'):
    return dict(claim_text='Disease D has higher aggressiveness.', source_context='',
        subject='Disease D', subject_source_mention='Disease D', relation=relation,
        object='aggressiveness', object_source_mention='aggressiveness', relation_trigger='has',
        subject_type='PlantDisease', object_type='PlantTrait', evidence_sentence_numbers=[1], certainty='descriptive',
        qualifiers={key:'' for key in ['crop_or_species','population','condition','comparator','metric']})


def test_trait_claim_does_not_fail_a_semantic_type_pair_rule():
    value={'claims':[claim()]};original=deepcopy(value)
    validate_schema(value, EXTRACTION_RESPONSE_SCHEMA)
    assert value == original


def test_enum_diagnostic_does_not_change_provisional_label():
    value={'claims':[claim()]}; value['claims'][0]['subject_type']='UnknownLabel'
    original=deepcopy(value)
    with pytest.raises(SchemaValidationError) as failure:
        validate_schema(value,EXTRACTION_RESPONSE_SCHEMA)
    message=str(failure.value)
    assert 'claims.0.subject_type' in message
    assert 'UnknownLabel' in message and 'Crop' in message and 'Unresolved' in message
    assert 'relation="has_trait"' in message
    assert value==original
    validate_schema({'claims':[claim('affects')]},EXTRACTION_RESPONSE_SCHEMA)


def test_required_diagnostics_list_only_missing_fields():
    with pytest.raises(SchemaValidationError,match='missing fields') as failure:
        validate_schema({'secret':'private response text'},{'type':'object','required':['claim_id']})
    assert 'claim_id' in str(failure.value) and 'private response text' not in str(failure.value)


@pytest.mark.parametrize('repeat_error',[False,True])
def test_schema_regeneration_is_bounded_and_receives_actionable_feedback(monkeypatch,repeat_error):
    metrics,progress,calls=[],[],[]
    client=OpenCodeClient(replace(Settings(),thinking={'extraction':False}),task='extraction',
                          on_request=metrics.append,on_progress=progress.append)
    def chat(*args):
        calls.append(args)
        result={'claims':[claim()]} if len(calls)==1 or repeat_error else {'claims':[]}
        if result['claims']:
            result['claims'][0]['subject_type']='UnknownLabel'
        return {'content':json.dumps(result),'finish_reason':'stop','usage':{
            'prompt_tokens':100,'completion_tokens':50,'completion_tokens_details':{'reasoning_tokens':0}}}
    monkeypatch.setattr(client._inference,'chat',chat)
    if repeat_error:
        with pytest.raises(LLMServiceError,match='has_trait'):
            client.chat_json('Extract faithful edges','original numbered evidence',max_tokens=4000,json_schema=EXTRACTION_RESPONSE_SCHEMA)
    else:
        assert client.chat_json('Extract faithful edges','original numbered evidence',max_tokens=4000,json_schema=EXTRACTION_RESPONSE_SCHEMA)=={'claims':[]}
    assert len(calls)==2 and calls[0][2]==calls[1][2]=='original numbered evidence'
    assert calls[0][4]==calls[1][4]  # Extra tokens cannot correct a type/relation rule.
    assert 'Do not relabel a disease as a crop' in calls[1][1]
    assert 'relation="has_trait"' in calls[1][1]
    assert metrics[0]['status']=='schema_error' and metrics[0]['failure_category']=='schema_error'
    assert metrics[0]['recovery_action']=='response_regeneration'
    assert all(row['requested_thinking']=='disabled' for row in metrics)
    assert 'Response violates schema' in progress[1]['provider_status']
    assert metrics[-1]['status']==('failed' if repeat_error else 'ok')
