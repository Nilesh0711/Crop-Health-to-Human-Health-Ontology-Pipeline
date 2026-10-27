"""Regressions for the source batch's misplaced annotation delimiter."""
import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient, format_request_metrics
from backend.providers.structured_output import StructuredOutputError, parse_json_object


SCHEMA = {
    'type': 'object', 'required': ['results'], 'additionalProperties': False,
    'properties': {'results': {
        'type': 'array', 'minItems': 1, 'maxItems': 1,
        'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['candidate_id', 'decision', 'alignment', 'quote'],
            'properties': {
                'candidate_id': {'type': 'string'},
                'decision': {'type': 'string', 'enum': ['entailed', 'needs_review']},
                'alignment': {
                    'type': 'object', 'required': ['explanation'], 'additionalProperties': False,
                    'properties': {'explanation': {'type': 'string'}},
                },
                'quote': {'type': 'string'},
            },
        },
    }},
}
ANSWER = {'results': [{
    'candidate_id': 'claim-1', 'decision': 'needs_review',
    'alignment': {'explanation': "The heading identifies the subject's context."},
    'quote': 'The measured response decreased under the reported conditions.',
}]}


def encode(answer):
    return json.dumps(answer, separators=(',', ':'))


def misplaced_brace(answer=ANSWER):
    valid = encode(answer)
    annotation = encode(answer['results'][0]['alignment'])
    assert annotation.endswith('"}')
    broken_annotation = annotation[:-2] + '}"'
    return valid.replace(annotation, broken_annotation, 1)


def test_annotation_delimiter_is_never_corrected_locally():
    with pytest.raises(StructuredOutputError):
        parse_json_object(misplaced_brace(), SCHEMA)


@pytest.mark.parametrize('wrapper', ['{}', ' \n{}\t '])
def test_valid_output_and_legitimate_annotation_brace_are_unchanged(wrapper):
    answer = deepcopy(ANSWER)
    answer['results'][0]['alignment']['explanation'] = 'An actual annotation containing a brace }'
    assert parse_json_object(wrapper.format(encode(answer)), SCHEMA) == (answer, [])


@pytest.mark.parametrize('wrapper', ['```json\n{}\n```', 'Response:\n{}'])
def test_wrappers_are_not_stripped_locally(wrapper):
    with pytest.raises(StructuredOutputError):
        parse_json_object(wrapper.format(encode(ANSWER)), SCHEMA)


@pytest.mark.parametrize('text', [
    encode(ANSWER)[:-1],  # Truncated output must never acquire a new delimiter.
    encode(ANSWER)[:-10],
    encode(ANSWER) + '{}',
    encode(ANSWER).replace("subject's", "subject\\'s"),
    '{"results":[],"results":[]}',
    '{"value":NaN}',
    '[{"results":[]}]',
])
def test_rejects_truncation_multiple_objects_invalid_escapes_and_ambiguity(text):
    with pytest.raises(StructuredOutputError):
        parse_json_object(text, SCHEMA)


def test_never_moves_a_brace_out_of_a_proof_quote():
    text = '{"quote":"Evidence text.}","decision":"needs_review"'
    schema = {'type': 'object', 'required': ['quote', 'decision'],
              'additionalProperties': False, 'properties': {
                  'quote': {'type': 'string'}, 'decision': {'type': 'string'}}}
    with pytest.raises(StructuredOutputError):
        parse_json_object(text, schema)


def test_correction_requires_a_supported_schema():
    with pytest.raises(StructuredOutputError):
        parse_json_object(misplaced_brace())
    with pytest.raises(StructuredOutputError):
        parse_json_object(misplaced_brace(), {**SCHEMA, 'allOf': []})


@pytest.mark.parametrize('change', ['missing_id', 'extra_field', 'invalid_decision', 'wrong_type'])
def test_syntactic_correction_cannot_accept_a_schema_violation(change):
    answer = deepcopy(ANSWER)
    row = answer['results'][0]
    if change == 'missing_id':
        del row['candidate_id']
    elif change == 'extra_field':
        row['unrequested'] = 'data'
    elif change == 'invalid_decision':
        row['decision'] = 'automatically_approved'
    else:
        row['candidate_id'] = False
    with pytest.raises(StructuredOutputError):
        parse_json_object(misplaced_brace(answer), SCHEMA)


def test_two_possible_schema_valid_corrections_are_rejected():
    text = '{"child":{"explanation":"one}","rationale":"two}","n":1}'
    scalar_properties = {'explanation': {'type': 'string'},
                         'rationale': {'type': 'string'}, 'n': {'type': 'integer'}}
    schema = {'type': 'object', 'properties': {
        **scalar_properties, 'child': {
            'type': 'object', 'required': ['explanation'], 'properties': scalar_properties}},
        'required': ['child']}
    with pytest.raises(StructuredOutputError):
        parse_json_object(text, schema)


def test_direct_api_cannot_silently_apply_a_local_correction():
    metrics = []
    client = OpenCodeClient(Settings(json_repair_attempts=0, json_regeneration_attempts=0), model='opencode-go/deepseek-v4.1-flash',
                            task='source', on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value={
            'content': misplaced_brace(), 'finish_reason': 'stop'}) as run:
        with pytest.raises(LLMServiceError, match='invalid JSON'):
            client.chat_json('system', 'input', json_schema=SCHEMA)
    run.assert_called_once()
    assert metrics[0]['model'] == 'opencode-go/deepseek-v4.1-flash'
    assert metrics[0]['next_model'] is None
    assert metrics[0]['attempt'] == 1
    assert metrics[0]['status'] == 'format_error'
    assert metrics[0]['response_format_adjustments'] == []


def test_unsafe_output_fails_visibly_without_a_retry_or_misleading_model_name():
    metrics = []
    client = OpenCodeClient(Settings(json_repair_attempts=0, json_regeneration_attempts=0), task='source', on_request=metrics.append)
    with patch.object(client._inference, 'chat', return_value={
            'content': encode(ANSWER)[:-1], 'finish_reason': 'stop'}) as run:
        with pytest.raises(LLMServiceError, match='invalid JSON') as error:
            client.chat_json('system', 'input', json_schema=SCHEMA)
    run.assert_called_once()
    assert 'Qwen' not in str(error.value)
    assert metrics[0]['status'] == 'format_error'
    assert metrics[0]['response_format_adjustments'] == []
    assert 'invalid JSON' in format_request_metrics(metrics[0])
