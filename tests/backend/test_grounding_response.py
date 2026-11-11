"""Wire-format regressions: never discard duplicates or misattach a mapping."""
import json
from copy import deepcopy

import pytest

from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.providers.structured_output import SchemaValidationError, parse_json_object
from backend.stages.ontology_grounding.contract import packet_schema
from backend.stages.ontology_grounding.grounder import catalog_term_map, ground_claims, request_packet
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from tests.backend.test_ontology_grounding import Grounder, NITRATE, ROOT, claim, reference  # noqa: F401


@pytest.mark.parametrize('fault', ['duplicate', 'missing', 'invented', 'extra'])
def test_results_require_exactly_one_entry_for_each_supplied_claim(reference, fault):
    packet, check = request_packet([claim('a'), claim('b')], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    if fault == 'duplicate':
        answer['results'][1]['claim_id'] = 'C1'
    elif fault == 'missing':
        answer['results'].pop()
    elif fault == 'invented':
        answer['results'][1]['claim_id'] = 'C99'
    else:
        answer['results'].append(deepcopy(answer['results'][0]))
    with pytest.raises(SchemaValidationError):
        check(answer)


def test_shared_item_schema_keeps_per_claim_candidate_ownership(reference):
    packet, check = request_packet([claim('a'), claim('b', subject='root', object='nitrate')], reference)
    _, _, term_iris, _, _ = catalog_term_map(reference)
    aliases = {iri: a for a, iri in term_iris.items()}
    packet['claims']['C1']['subject_candidates'] = [aliases[NITRATE]]
    packet['claims']['C2']['subject_candidates'] = [aliases[ROOT]]
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    answer['results'][0]['subject']['term'] = packet['claims']['C2']['subject_candidates'][0]
    # The alias exists in the packet but was not offered for C1's subject.
    with pytest.raises(SchemaValidationError, match='C1.subject'):
        check(answer)


def test_grounding_bindings_finish_with_constrained_choices_instead_of_open_text(reference):
    packet, _ = request_packet([claim()], ConceptIndex(reference))
    properties = packet_schema(packet)['properties']['results']['items']['properties']
    for role in ('subject', 'object', 'relation'):
        binding = properties[role]
        assert list(binding['properties']) == ['reason', 'mapping', 'term']
        assert binding['required'] == ['reason', 'mapping', 'term']
        assert 'enum' in binding['properties']['mapping']
        assert 'enum' in binding['properties']['term']
        assert binding['additionalProperties'] is False


def test_valid_escaped_control_characters_and_external_whitespace_are_preserved():
    value = {'reason': 'A quoted annotation with\ta tab and\na new line.'}
    encoded = '\t\n' + json.dumps(value) + '\n\t'
    assert parse_json_object(encoded) == (value, [])


@pytest.mark.parametrize('character', ['\t', '\n', '\x00'])
def test_control_characters_inside_grounding_strings_use_only_last_resort_regeneration(tmp_path, reference, monkeypatch, character):
    packet, _ = request_packet([claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    valid = json.dumps(answer)
    malformed = valid.replace('Synthetic exact-name alignment.', 'Synthetic' + character + 'alignment.', 1)
    calls, metrics = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly', on_request=metrics.append)
    def chat(model, system, user, schema, output_limit):
        calls.append((model, user, schema, output_limit))
        return {'content': malformed if len(calls) == 1 else valid, 'finish_reason': 'stop',
                'usage': {}, 'response_format': 'json_schema'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    rows = ground_claims([claim()], tmp_path, reference, client)
    assert len(rows) == 1 and len(calls) == 2 and calls[0] == calls[1]
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert metrics[0]['failure_category'] == 'response_structure_error'
    assert metrics[1]['recovery_completed']
    assert not any(m['response_format_adjustments'] for m in metrics)


def test_failed_control_character_recovery_stops_without_third_call_or_formatting_patch(tmp_path, reference, monkeypatch):
    packet, _ = request_packet([claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    malformed = json.dumps(answer).replace('Synthetic exact-name alignment.', 'Synthetic\talignment.', 1)
    calls, metrics = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly', on_request=metrics.append)
    def chat(*args):
        calls.append(args)
        return {'content': malformed, 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_schema'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    with pytest.raises(LLMServiceError, match='unescaped control character inside a string'):
        ground_claims([claim()], tmp_path, reference, client)
    assert len(calls) == 2
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert not (tmp_path / 'ontology_grounding.jsonl').exists()


@pytest.mark.parametrize('role', ['subject', 'object', 'relation'])
def test_mapping_objects_never_accept_entity_classification_fields(reference, role):
    packet, check = request_packet([claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    answer['results'][0][role]['denotation'] = 'concept'
    with pytest.raises(SchemaValidationError, match='denotation'):
        check(answer)


@pytest.mark.parametrize('fault', ['missing', 'invalid'])
def test_entity_classifications_are_required_and_not_filled_locally(reference, fault):
    packet, check = request_packet([claim()], ConceptIndex(reference))
    answer = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    if fault == 'missing':
        answer['results'][0].pop('subject_denotation')
    else:
        answer['results'][0]['subject_denotation'] = 'relationship'
    with pytest.raises(SchemaValidationError, match='subject_denotation'):
        check(answer)


def test_independent_entity_classifications_preserve_occurrence_identity(tmp_path, reference):
    class MixedGrounder(Grounder):
        def chat_json(self, *args, **kwargs):
            answer = super().chat_json(*args, **kwargs)
            answer['results'][0]['subject_denotation'] = 'state'
            answer['results'][0]['object_denotation'] = 'particular'
            return answer
    siblings = [claim(k, qualifiers={}, source_evidence_sentence_numbers=[1]) for k in ('a', 'b')]
    rows = ground_claims(siblings, tmp_path, reference, MixedGrounder(), batch_size=2)
    first, second = rows[0]['bindings'], rows[1]['bindings']
    assert first['subject']['denotation'] == 'state'
    assert first['object']['denotation'] == 'particular'
    for role in ['subject', 'object']:
        assert first[role]['entity_id'] == first[role]['occurrence_id']
        assert second[role]['denotation'] == 'concept'
        assert second[role]['entity_id'].startswith('mention-')  # Conflicting sibling classifications are held.
    assert 'denotation' not in first['relation']


def test_complete_but_inconsistent_selection_does_not_double_generation_budget(tmp_path, reference, monkeypatch):
    packet, _ = request_packet([claim()], ConceptIndex(reference))
    valid = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    invalid = deepcopy(valid)
    invalid['results'][0]['subject']['mapping'] = 'unresolved'
    assert invalid['results'][0]['subject']['term'] is not None
    calls = []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly')
    def chat(model, system, user, schema, output_limit):
        calls.append(output_limit)
        return {'content': json.dumps(invalid if len(calls) == 1 else valid), 'finish_reason': 'stop',
                'usage': {}, 'response_format': 'json_object'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    rows = ground_claims([claim()], tmp_path, reference, client)
    assert len(rows) == 1 and calls == [16096, 16096]


def test_out_of_order_results_bind_to_claim_ids_not_array_positions(tmp_path, reference):
    class ReversedGrounder(Grounder):
        def chat_json(self, *args, **kwargs):
            answer = super().chat_json(*args, **kwargs)
            answer['results'].reverse()
            return answer
    rows = ground_claims([claim('a'), claim('b', subject='root', object='nitrate')],
                        tmp_path, reference, ReversedGrounder(), batch_size=2)
    assert [r['candidate_id'] for r in rows] == ['a', 'b']
    assert rows[0]['bindings']['subject']['iri'] == NITRATE
    assert rows[1]['bindings']['subject']['iri'] == ROOT
    assert rows[0]['original_claim']['subject'] == 'nitrate'
    assert rows[1]['original_claim']['subject'] == 'root'


@pytest.mark.parametrize('duplicate', ['property', 'claim_id'])
def test_duplicate_output_regenerates_once_from_original_evidence(tmp_path, reference, monkeypatch, duplicate):
    packet, _ = request_packet([claim('a'), claim('b')], ConceptIndex(reference))
    valid = Grounder().chat_json('', json.dumps(packet), response_check=lambda _: None)
    if duplicate == 'property':
        # Reproduce the actual failure pattern without retaining a real paper's claims.
        invalid = json.dumps(valid).replace('"claim_id": "C1"', '"claim_id": "C1", "claim_id": "C1"')
    else:
        repeated = deepcopy(valid)
        repeated['results'][1]['claim_id'] = 'C1'
        invalid = json.dumps(repeated)
    calls, metrics = [], []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly', on_request=metrics.append)
    def chat(model, system, user, schema, output_limit):
        calls.append((model, user))
        return {'content': invalid if len(calls) == 1 else json.dumps(valid),
                'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    rows = ground_claims([claim(k, source_evidence_sentence_numbers=[1]) for k in ('a', 'b')], tmp_path, reference, client, batch_size=2)
    assert len(rows) == 2 and len(calls) == 2 and calls[0] == calls[1]
    assert metrics[0]['recovery_pending'] and metrics[1]['recovery_completed']
    assert packet_schema(packet)['properties']['results']['items']['required'][0] == 'claim_id'


def test_exhausted_duplicate_recovery_fails_as_grounding_without_saving(tmp_path, reference, monkeypatch):
    calls = []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly')
    def chat(*args):
        calls.append(args)
        return {'content': '{"results":[],"results":[]}', 'finish_reason': 'stop',
                'usage': {}, 'response_format': 'json_object'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    with pytest.raises(LLMServiceError, match='ontology grounding model .*Duplicate JSON property: results'):
        ground_claims([claim()], tmp_path, reference, client)
    assert len(calls) == 2
    assert not (tmp_path / 'ontology_grounding.jsonl').exists()
