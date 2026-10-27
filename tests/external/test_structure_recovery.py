"""Ambiguous JSON nesting regenerates without weakening scientific edit guards."""
import json
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError, request_log_level
from backend.providers.response_repair import apply_model_patch
from backend.providers.structured_output import ResponseStructureError, StructuredOutputError, parse_json_object


SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['assessment', 'finding'], 'properties': {
        'assessment': {'type': 'object', 'additionalProperties': False,
            'required': ['bindings', 'evidence', 'decision'], 'properties': {
                'bindings': {'type': 'object', 'additionalProperties': False, 'required': ['condition'],
                    'properties': {'condition': {'type': 'object', 'additionalProperties': False,
                        'required': ['explanation'], 'properties': {'explanation': {'type': 'string'}}}}},
                'evidence': {'type': 'array', 'items': {'type': 'string'}},
                'decision': {'type': 'string', 'enum': ['insufficient', 'supported']}}},
        'finding': {'type': 'object', 'additionalProperties': False, 'required': ['summary'],
            'properties': {'summary': {'type': 'string'}}}}}
ANSWER = {'assessment': {'bindings': {'condition': {'explanation': 'The context does not establish the endpoint.'}},
                         'evidence': [], 'decision': 'insufficient'},
          'finding': {'summary': 'No compatible outcome was established.'}}
VALID = json.dumps(ANSWER, separators=(',', ':'))
BOUNDARY = '"explanation":"The context does not establish the endpoint."'
BROKEN = VALID.replace(BOUNDARY + '}}', BOUNDARY + '}}}', 1)


def reply(text):
    return {'content': text, 'finish_reason': 'stop'}


def test_prematurely_closed_root_requires_regeneration_instead_of_separator_edits():
    with pytest.raises(ResponseStructureError, match='ownership is ambiguous'):
        parse_json_object(BROKEN)
    with pytest.raises(ResponseStructureError):
        parse_json_object(VALID + VALID)
    # An excess terminal closer still has an ordinary formatting repair path.
    with pytest.raises(StructuredOutputError) as error:
        parse_json_object(VALID + '}')
    assert not isinstance(error.value, ResponseStructureError)


def test_ambiguous_nesting_regenerates_once_with_the_same_assigned_model_and_original_evidence():
    settings = Settings()
    metrics = []
    client = OpenCodeClient(settings, task='external', on_request=metrics.append)
    evidence = 'Original numbered paper excerpts; retain conditions and negative findings.'
    with patch.object(client._inference, 'chat', side_effect=[reply(BROKEN), reply(VALID)]) as call:
        answer = client.chat_json('Judge only supplied evidence.', evidence, json_schema=SCHEMA,
                                  model=settings.external_paper_model)
    assert answer == ANSWER and call.call_count == 2
    first, second = [c.args for c in call.call_args_list]
    assert first[0] == second[0] == settings.external_paper_model
    assert first[2] == second[2] == evidence and first[3] == second[3] == SCHEMA
    assert first[4] == second[4]  # Additional tokens cannot repair nesting.
    assert BROKEN not in second[1] and BROKEN not in second[2]
    assert 'exact object/array nesting' in second[1]
    assert metrics[0]['status'] == 'response_structure_error' and metrics[0]['recovery_pending']
    assert [request_log_level(m) for m in metrics] == ['warning', 'info']
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert metrics[1]['recovery_completed'] and client.thinking_enabled is False
    assert all(not m['response_format_adjustments'] for m in metrics)


def test_failed_structure_regeneration_does_not_start_a_formatting_repair_or_third_call():
    client = OpenCodeClient(Settings(), task='external')
    with patch.object(client._inference, 'chat', return_value=reply(BROKEN)) as call:
        with pytest.raises(LLMServiceError, match='ownership is ambiguous'):
            client.chat_json('Rules.', 'Original evidence.', json_schema=SCHEMA)
    assert call.call_count == 2


def test_structure_regeneration_respects_its_independent_disable_setting():
    client = OpenCodeClient(Settings(json_regeneration_attempts=0), task='source')
    with patch.object(client._inference, 'chat', return_value=reply(BROKEN)) as call:
        with pytest.raises(LLMServiceError):
            client.chat_json('Rules.', 'Original evidence.', json_schema=SCHEMA)
    assert call.call_count == 1


def test_safe_edits_cannot_reparent_nested_scientific_fields_even_when_the_result_fits_the_schema():
    # A valid-looking output alone does not certify field ownership in the
    # malformed original. Its meaning must be judged again from the evidence.
    proposal = {'edits': [{'before': BOUNDARY + '}}}', 'after': BOUNDARY + '}}'}]}
    with pytest.raises(StructuredOutputError, match='ambiguous nesting'):
        apply_model_patch(BROKEN, proposal, SCHEMA)


def test_repair_that_removes_too_many_closers_reports_invalid_json_instead_of_changed_evidence():
    proposal = {'edits': [{'before': BOUNDARY + '}}}', 'after': BOUNDARY + '}'}]}
    with pytest.raises(StructuredOutputError, match='did not produce a complete valid JSON object'):
        apply_model_patch(BROKEN, proposal, SCHEMA)


def test_simple_separator_errors_still_use_only_a_model_proposed_formatting_patch():
    broken = VALID.replace('[],"decision"', '[] "decision"')
    proposal = {'edits': [{'before': '[] "decision"', 'after': '[],"decision"'}]}
    metrics = []
    client = OpenCodeClient(Settings(), task='source', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=[reply(broken), reply(json.dumps(proposal))]) as call:
        assert client.chat_json('Rules.', 'Original evidence.', json_schema=SCHEMA) == ANSWER
    assert call.call_count == 2 and metrics[-1]['purpose'] == 'format_repair'


def test_regenerated_paper_response_still_withholds_invented_sentence_ids():
    from copy import deepcopy
    from backend.stages.external_validation.synthesis import judge_paper
    from tests.external.test_paper_synthesis import example

    claim, document, good = example()
    invalid = deepcopy(good)
    invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    metrics = []
    settings = Settings()
    client = OpenCodeClient(settings, model=settings.external_paper_model, task='external', on_request=metrics.append)
    multiple_roots = json.dumps(good) + json.dumps(good)
    with patch.object(client._inference, 'chat', side_effect=[reply(multiple_roots), reply(json.dumps(invalid))]) as call:
        decision, _ = judge_paper(client, claim, document, settings)
    assert call.call_count == 2 and decision['decision'] == 'needs_review'
    assert decision['evidence'] == []
    assert [m['status'] for m in metrics] == ['response_structure_error', 'evidence_needs_review']
