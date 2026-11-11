"""Empty annotations use one explicit model patch; scientific values never change."""
from copy import deepcopy
import json

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.response_repair import validate_schema
from backend.providers.schema_annotations import apply_model_removals
from backend.providers.structured_output import NullAnnotationError, SchemaValidationError, StructuredOutputError, EvidenceSelectionError


SCHEMA = {'type': 'object', 'properties': {'results': {'type': 'array', 'items': {
    'type': 'object', 'properties': {'claim_id': {'type': 'string'}, 'binding': {
        'type': 'object', 'properties': {'term': {'type': ['string', 'null']},
                                       'reason': {'type': 'string'}},
        'required': ['term', 'reason'], 'additionalProperties': False}},
    'required': ['claim_id', 'binding'], 'additionalProperties': False}}},
    'required': ['results'], 'additionalProperties': False}
ANSWER = {'results': [{'claim_id': f'C{i}', 'binding': {'term': None, 'reason': 'No defensible identity.'}} for i in range(1, 7)]}


def malformed():
    data = deepcopy(ANSWER)
    for row in data['results']:
        row['binding']['mapping_note'] = None
    return data


def paths():
    return [['results', i, 'binding', 'mapping_note'] for i in range(6)]


def reply(data):
    return {'content': json.dumps(data), 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}


def test_only_schema_extra_null_fields_are_eligible():
    original = malformed()
    with pytest.raises(NullAnnotationError) as failure:
        validate_schema(original, SCHEMA)
    assert failure.value.paths == paths()
    assert original == malformed()  # Validation proposes no local edit.


@pytest.mark.parametrize('value', ['', 'explanatory text', 0, False, {}, []])
def test_non_null_annotations_are_never_discardable(value):
    data = malformed()
    data['results'][0]['binding']['mapping_note'] = value
    with pytest.raises(SchemaValidationError) as failure:
        validate_schema(data, SCHEMA)
    assert not isinstance(failure.value, NullAnnotationError)


def test_missing_scientific_content_requires_regeneration_instead():
    data = malformed(); data['results'][0]['binding'].pop('reason')
    with pytest.raises(SchemaValidationError) as failure:
        validate_schema(data, SCHEMA)
    assert not isinstance(failure.value, NullAnnotationError)


@pytest.mark.parametrize('bad_paths', [[], paths()[:-1], [*paths()[:-1], paths()[0]],
    [*paths()[:-1], ['results', 5, 'binding', 'term']]])
def test_patch_cannot_delete_known_fields_duplicate_paths_or_omit_edits(bad_paths):
    data = malformed()
    with pytest.raises(StructuredOutputError):
        apply_model_removals(data, {'remove_paths': bad_paths}, paths())
    assert data == malformed()


def test_one_small_same_model_patch_preserves_all_decisions_and_raw_audit(monkeypatch, tmp_path):
    metrics, calls, guards = [], [], []
    client = OpenCodeClient(Settings(), task='pathway_assembly', on_request=metrics.append, response_dir=tmp_path)
    replies = iter([reply(malformed()), reply({'remove_paths': paths()})])
    def chat(*args):
        calls.append(args)
        return next(replies)
    monkeypatch.setattr(client._inference, 'chat', chat)
    client.set_request_context({'stage': 'ontology_grounding'})
    result = client.chat_json('Ground the supplied concepts.', 'Original scientific source packet.',
        max_tokens=12000, json_schema=SCHEMA, response_check=lambda result: guards.append(result))
    assert result == ANSWER and guards == [ANSWER]
    assert len(calls) == 2 and calls[0][0] == calls[1][0] == client.model
    assert calls[1][-1] == 4096
    assert 'Original scientific source packet' not in calls[1][2]
    assert [m['purpose'] for m in metrics] == ['judgement', 'format_repair']
    assert metrics[0]['recovery_action'] == 'null_annotation_repair' and metrics[1]['recovery_completed']
    assert metrics[1]['response_format_adjustments'][0]['kind'] == 'model_null_annotation_patch'
    archives = [json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.glob('*.json')]
    first = next(a for a in archives if a['purpose'] == 'judgement')
    second = next(a for a in archives if a['purpose'] == 'format_repair')
    assert json.loads(first['content']) == malformed() and first['accepted_response'] is None
    assert second['accepted_response'] == ANSWER
    assert second['format_adjustments'][0]['source_response_artifact'] == metrics[0]['response_artifact']


def test_failed_patch_stops_after_two_calls_without_another_recovery(monkeypatch):
    client = OpenCodeClient(Settings(), task='pathway_assembly')
    calls = []
    def chat(*args):
        calls.append(args)
        return reply(malformed() if len(calls) == 1 else {'remove_paths': [['results', 0, 'binding', 'term']]})
    monkeypatch.setattr(client._inference, 'chat', chat)
    with pytest.raises(LLMServiceError):
        client.chat_json('Check.', 'Source.', json_schema=SCHEMA)
    assert len(calls) == 2


def test_repaired_format_cannot_override_an_evidence_guard(monkeypatch):
    client = OpenCodeClient(Settings(), task='external')
    replies = iter([reply(malformed()), reply({'remove_paths': paths()})])
    monkeypatch.setattr(client._inference, 'chat', lambda *args: next(replies))
    def guard(_):
        raise EvidenceSelectionError('Evidence does not establish this finding')
    result = client.chat_json('Check.', 'Source.', json_schema=SCHEMA, response_check=guard)
    assert result == ANSWER
    assert client.last_request_metrics['status'] == 'evidence_needs_review'


def test_non_null_extras_keep_existing_regeneration_route(monkeypatch):
    invalid = malformed(); invalid['results'][0]['binding']['mapping_note'] = 'Content that cannot be discarded.'
    metrics, calls = [], []
    client = OpenCodeClient(Settings(), task='pathway_assembly', on_request=metrics.append)
    replies = iter([reply(invalid), reply(ANSWER)])
    def chat(*args):
        calls.append(args)
        return next(replies)
    monkeypatch.setattr(client._inference, 'chat', chat)
    assert client.chat_json('Check.', 'Original source.', json_schema=SCHEMA) == ANSWER
    assert len(calls) == 2 and calls[0][2] == calls[1][2]
    assert metrics[0]['recovery_action'] == 'response_regeneration'
