"""Plain grounding records retain every semantic decision and source binding."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from backend.config.settings import Settings
from backend.providers.inference import InferenceHTTP
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.structured_output import SchemaValidationError
from backend.shared.storage import read_jsonl
from backend.stages.ontology_grounding.grounder import catalog_term_map, ground_claims, request_packet
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from backend.stages.ontology_grounding.rows import parse_rows, instructions, required_ids, VERSION
from tests.backend.test_ontology_grounding import Grounder, claim, reference  # noqa: F401


def encode(answer):
    lines = []
    for row in answer['results']:
        key = row['claim_id']
        lines.append('\t'.join([key + '.scope', 'scope', row['scope'], '-', row['scope_reason']]))
        for role in ('subject', 'object', 'relation'):
            binding = row[role]
            lines.append('\t'.join([key + '.' + role, 'relation' if role == 'relation' else row[role + '_denotation'],
                binding['mapping'], binding['term'] if binding['term'] is not None else '-', binding['reason']]))
    return '\n'.join(lines)


def fixture_packet(reference, claims=None):
    payload, _ = request_packet(claims or [claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(payload), response_check=lambda _: None)
    return payload, answer


def response(text, identifier='provider-row'):
    return {'content': text, 'finish_reason': 'stop', 'usage': {}, 'response_format': 'text', 'provider_request_id': identifier}


def test_default_is_json_and_bad_configuration_is_rejected():
    assert Settings().ontology_grounding_output_format == 'json'
    with pytest.raises(ValueError, match='OUTPUT_FORMAT'):
        replace(Settings(), ontology_grounding_output_format='csv')


def test_five_columns_preserve_all_fields_and_out_of_order_records(reference):
    payload, answer = fixture_packet(reference, [claim('a'), claim('b', subject='root', object='nitrate')])
    answer['results'][0]['subject']['reason'] = 'A "quoted" name, braces {x}, punctuation | and Unicode \u03b1 are data.'
    text = encode(answer)
    assert len(text.splitlines()) == 8
    assert all(len(line.split('\t')) == 5 for line in text.splitlines())
    assert parse_rows(text, payload) == answer
    assert parse_rows('\r\n'.join(reversed(text.splitlines())) + '\r\n', payload) == answer
    assert all(identifier in instructions(payload) for identifier in required_ids(payload))


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'invented', 'header', 'blank', 'fence', 'spaces',
    'extra_column', 'reason_tab', 'reason_newline', 'empty_reason', 'long_reason', 'control', 'bad_scope_kind',
    'scope_target', 'relation_denotation', 'wrong_denotation', 'unresolved_target', 'missing_target', 'invented_target'])
def test_rows_fail_closed_without_repair_or_partial_selection(reference, fault):
    payload, answer = fixture_packet(reference)
    lines = encode(answer).split('\n')
    fields = [line.split('\t') for line in lines]
    if fault == 'missing': lines.pop()
    elif fault == 'duplicate': lines.append(lines[0])
    elif fault == 'invented': lines[0] = lines[0].replace('C1.scope', 'C99.scope')
    elif fault == 'header': lines.insert(0, 'record_id\tkind\tdecision\ttarget\treason')
    elif fault == 'blank': lines.append('')
    elif fault == 'fence': lines = ['```text', *lines, '```']
    elif fault == 'spaces': lines[0] = lines[0].replace('\t', '    ')
    else:
        if fault == 'extra_column': fields[1].append('extra')
        elif fault == 'reason_tab': fields[1][4] += '\textra'
        elif fault == 'reason_newline': fields[1][4] += '\nextra'
        elif fault == 'empty_reason': fields[1][4] = ' '
        elif fault == 'long_reason': fields[1][4] = 'x' * 601
        elif fault == 'control': fields[1][4] += '\x00'
        elif fault == 'bad_scope_kind': fields[0][1] = 'concept'
        elif fault == 'scope_target': fields[0][3] = 'T1'
        elif fault == 'relation_denotation': fields[3][1] = 'concept'
        elif fault == 'wrong_denotation': fields[1][1] = 'class'
        elif fault == 'unresolved_target': fields[1][2] = 'unresolved'
        elif fault == 'missing_target': fields[1][3] = '-'
        elif fault == 'invented_target': fields[1][3] = 'T999'
        lines = ['\t'.join(row) for row in fields]
    text = '\n'.join(lines) + ('\n' if fault == 'blank' else '')
    with pytest.raises(SchemaValidationError):
        parse_rows(text, payload)


def test_rows_enforce_candidate_ownership_even_when_an_alias_exists_elsewhere(reference):
    from tests.backend.test_ontology_grounding import NITRATE, ROOT
    payload, _ = request_packet([claim('a'), claim('b', subject='root', object='nitrate')], reference)
    # The parser still rejects mismatched selections in an explicitly restricted
    # historical packet, although active packets now offer all approved classes.
    _, _, term_iris, _, _ = catalog_term_map(reference)
    aliases = {iri: a for a, iri in term_iris.items()}
    payload['claims']['C1']['subject_candidates'] = [aliases[NITRATE]]
    payload['claims']['C1']['object_candidates'] = [aliases[ROOT]]
    answer = Grounder().chat_json('', json.dumps(payload), response_check=lambda _: None)
    answer['results'][0]['subject']['term'] = payload['claims']['C1']['object_candidates'][0]
    with pytest.raises(SchemaValidationError, match='C1.subject'):
        parse_rows(encode(answer), payload)


def test_text_transport_omits_json_mode_and_tools_even_when_json_override_exists(monkeypatch):
    captured = []
    def request(self, method, url, **kwargs):
        captured.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'plain\trows'}}]}
    monkeypatch.setattr(InferenceHTTP, '_request', request)
    result = InferenceHTTP(Settings(opencode_response_format='json_schema'), thinking_enabled=False).chat_text(
        Settings().pathway_assembly_model, 'Plain rows only.', 'Source data.', 8192)
    assert 'response_format' not in captured[0] and 'tools' not in captured[0]
    assert captured[0]['thinking'] == {'type': 'disabled'}
    assert result['content'] == 'plain\trows' and result['response_format'] == 'text'


def test_grounding_rows_make_one_call_and_keep_raw_output_and_source(tmp_path, reference, monkeypatch):
    source = claim(); original = deepcopy(source)
    payload, answer = fixture_packet(reference)
    text = encode(answer)
    metrics, calls = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='tsv'), task='pathway_assembly', response_dir=tmp_path / 'responses', on_request=metrics.append)
    def chat(model, system, user, limit):
        calls.append((model, system, user, limit))
        return response(text)
    monkeypatch.setattr(client._inference, 'chat_text', chat)
    monkeypatch.setattr(client._inference, 'chat', lambda *_: pytest.fail('No automatic JSON-format fallback'))
    monkeypatch.setattr('backend.providers.llm.parse_json_object', lambda *_: pytest.fail('Rows must not use a JSON parser'))
    rows = ground_claims([source], tmp_path, reference, client)
    assert source == original and len(calls) == 1
    assert json.loads(calls[0][2]) == payload
    assert 'Response JSON Schema:' not in calls[0][1]
    assert rows[0]['original_claim'] == original and rows[0]['output_protocol'] == VERSION
    assert rows[0]['response_format'] == 'text'
    archive = json.loads(next((tmp_path / 'responses').glob('*.json')).read_text(encoding='utf-8'))
    assert archive['content'] == text and archive['accepted_response'] == text
    assert metrics[0]['status'] == 'ok' and metrics[0]['purpose'] == 'judgement'
    # Completed compatible rows reuse their decisions without another model call.
    assert ground_claims([source], tmp_path, reference, client) == rows
    assert len(calls) == 1 and len(read_jsonl(tmp_path / 'ontology_grounding.jsonl')) == 1


@pytest.mark.parametrize('recover', [True, False])
def test_invalid_rows_have_only_one_last_resort_response_without_json_repair(tmp_path, reference, monkeypatch, recover):
    _, answer = fixture_packet(reference)
    valid = encode(answer); invalid = valid.split('\n')[0]
    calls, metrics = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='tsv'), task='pathway_assembly', on_request=metrics.append)
    def chat(model, system, user, limit):
        calls.append((model, user, limit))
        return response(valid if recover and len(calls) == 2 else invalid)
    monkeypatch.setattr(client._inference, 'chat_text', chat)
    monkeypatch.setattr(client._inference, 'chat', lambda *_: pytest.fail('No JSON repair/fallback'))
    if recover:
        assert len(ground_claims([claim()], tmp_path, reference, client)) == 1
    else:
        with pytest.raises(LLMServiceError, match='missing required record IDs'):
            ground_claims([claim()], tmp_path, reference, client)
        assert not (tmp_path / 'ontology_grounding.jsonl').exists()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']


def test_disabled_recovery_really_makes_one_call(tmp_path, reference, monkeypatch):
    client = OpenCodeClient(replace(Settings(), ontology_grounding_output_format='tsv', json_regeneration_attempts=0), task='pathway_assembly')
    calls = []
    def chat(*args):
        calls.append(args)
        return response('C1.scope\tscope\tin_scope\t-\tScope only.')
    monkeypatch.setattr(client._inference, 'chat_text', chat)
    with pytest.raises(LLMServiceError):
        ground_claims([claim()], tmp_path, reference, client)
    assert len(calls) == 1


def test_changing_explicit_wire_format_reassesses_model_results_without_mutating_source(tmp_path, reference, monkeypatch):
    source = claim()
    _, answer = fixture_packet(reference)
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly')
    json_calls, row_calls = [], []
    def json_chat(*args):
        json_calls.append(args)
        return {**response(json.dumps(answer)), 'response_format': 'json_schema'}
    monkeypatch.setattr(client._inference, 'chat', json_chat)
    before = ground_claims([source], tmp_path, reference, client)
    rows_client = OpenCodeClient(Settings(ontology_grounding_output_format='tsv'), task='pathway_assembly')
    def row_chat(*args):
        row_calls.append(args)
        return response(encode(answer))
    monkeypatch.setattr(rows_client._inference, 'chat_text', row_chat)
    after = ground_claims([source], tmp_path, reference, rows_client)
    assert len(json_calls) == len(row_calls) == 1
    assert before[0]['config_signature'] != after[0]['config_signature']
    assert before[0]['bindings'] == after[0]['bindings']
    assert before[0]['original_claim'] == after[0]['original_claim'] == source


@pytest.mark.parametrize('fail_json', [False, True])
def test_format_comparison_freezes_identical_inputs_with_no_recovery_or_source_mutation(tmp_path, reference, monkeypatch, fail_json):
    from backend.stages.evaluation.grounding_formats import compare_formats
    claims = [claim('a'), claim('b', qualifiers={'condition': 'Different condition'})]
    original = deepcopy(claims)
    observed = []
    def factory(wire_format, log, response_dir):
        settings = replace(Settings(), ontology_grounding_output_format=wire_format,
                           json_repair_attempts=0, json_regeneration_attempts=0)
        client = OpenCodeClient(settings, task='pathway_assembly', on_request=log, response_dir=response_dir)
        def answer(user):
            observed.append((wire_format, user))
            return Grounder().chat_json('', user, response_check=lambda _: None)
        def json_chat(model, system, user, schema, limit):
            value = answer(user)
            if fail_json:
                value = {'results': []}
            return {**response(json.dumps(value)), 'response_format': 'json_schema'}
        monkeypatch.setattr(client._inference, 'chat', json_chat)
        monkeypatch.setattr(client._inference, 'chat_text', lambda model, system, user, limit: response(encode(answer(user))))
        return client
    output = tmp_path / 'comparison'
    report = compare_formats(claims, reference, ConceptIndex(reference), output, factory, batches=1)
    assert claims == original and len(observed) == 2 and observed[0][1] == observed[1][1]
    assert report['summary']['tsv']['valid_first_pass'] == 1
    assert report['summary']['json']['valid_first_pass'] == (0 if fail_json else 1)
    assert report['scientific_accuracy_evaluated'] is False and report['recovery_disabled'] is True
    assert all(m['recovery_requests'] == 0 and m['requests'] == 1 for m in report['summary'].values())
    assert report['differing_decision_fields'] == 0
    assert report['compared_decision_fields'] == (0 if fail_json else 18)
    assert read_jsonl(output / 'source_claims.jsonl') == original
    with pytest.raises(ValueError, match='not empty'):
        compare_formats(claims, reference, ConceptIndex(reference), output, factory, batches=1)
