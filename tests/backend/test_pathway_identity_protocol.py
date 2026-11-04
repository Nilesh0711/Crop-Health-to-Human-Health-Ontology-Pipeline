"""Synthetic identity contracts: exclusive assignments, exact context, resumability."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import IncompleteResponseError, SchemaValidationError
from backend.stages.pathway_assembly.contract import SYSTEM
from backend.stages.pathway_assembly.protocol import IdentityPacket, check_groups
from backend.stages.pathway_assembly.assembly import assemble_pathways
from tests.backend.test_pathway_assembly import saved_claim, Aligner, configuration


def packet():
    text = 'Member R changes Process P under Condition K.'
    batch = [{'endpoint_id': 'stored-long-endpoint-a', 'edge_id': 'stored-long-edge', 'name': 'Member R',
              'evidence': text, 'local_context': text, 'qualifiers': {'condition': 'Condition K'}},
             {'endpoint_id': 'stored-long-endpoint-b', 'edge_id': 'stored-long-edge', 'name': 'Class Q',
              'evidence': text, 'local_context': 'Class Q contains several distinct members.'}]
    registry = {'stored-long-node': {'node_id': 'stored-long-node', 'canonical_label': 'Class Q',
                'identity_context': [{'paper': 'Document A', 'evidence': text, 'local_context': text}],
                'entity_types': ['Unresolved'], 'aliases': ['Class Q'], 'occurrence_count': 1}}
    return IdentityPacket(batch, registry, {})


def answer():
    return {'endpoint_assignments': {'E1': 1, 'E2': 2}, 'registry_assignments': {'N1': 2},
            'groups': [{'group_id': 1, 'joins_existing_registry_node': False, 'canonical_label': 'Member R', 'explanation': 'The named member has its own identity.'},
                       {'group_id': 2, 'joins_existing_registry_node': True, 'canonical_label': 'Class Q', 'explanation': 'The broad class is the existing referent.'}]}


def test_one_assignment_per_endpoint_translates_to_original_ids_without_local_semantic_choices():
    request = packet()
    before = deepcopy(answer())
    groups = request.groups(before)
    assert groups[0]['endpoint_ids'] == ['stored-long-endpoint-a']
    assert groups[0]['existing_node_ids'] == []
    assert groups[1]['endpoint_ids'] == ['stored-long-endpoint-b']
    assert groups[1]['existing_node_ids'] == ['stored-long-node']
    assert before == answer()
    assert 'Category membership is not entity identity' in SYSTEM
    assert 'stored-long-' not in request.user


def test_exact_context_is_shared_without_summarizing_or_losing_conditions():
    request = packet()
    data = request.payload
    shared = data['incoming_endpoints'][0]['evidence_ref']
    assert shared == data['incoming_endpoints'][0]['local_context_ref']
    assert shared == data['registry_nodes'][0]['identity_context'][0]['evidence_ref']
    assert data['source_texts'][shared] == 'Member R changes Process P under Condition K.'
    assert data['incoming_endpoints'][0]['qualifiers'] == {'condition': 'Condition K'}
    assert len(data['source_texts']) == 2
    assert request.input_characters(SYSTEM) == len(SYSTEM) + len(request.user) + len(json.dumps(request.schema, ensure_ascii=False))


@pytest.mark.parametrize('problem', ['missing_endpoint', 'unknown_endpoint', 'endpoint_list', 'unknown_node',
                                    'missing_node', 'invalid_group', 'wrong_protocol'])
def test_request_specific_schema_rejects_invalid_or_overlapping_assignment_shapes(problem):
    value = answer()
    if problem == 'missing_endpoint': value['endpoint_assignments'].pop('E2')
    elif problem == 'unknown_endpoint': value['endpoint_assignments']['E999'] = 1
    elif problem == 'endpoint_list': value['endpoint_assignments']['E1'] = [1, 2]
    elif problem == 'unknown_node': value['registry_assignments']['N999'] = 1
    elif problem == 'missing_node': value['registry_assignments'].pop('N1')
    elif problem == 'invalid_group': value['endpoint_assignments']['E1'] = 999
    else: value = {'groups': [{'canonical_label': 'Class Q', 'endpoint_ids': ['E1'], 'existing_node_ids': []}]}
    with pytest.raises(SchemaValidationError):
        packet().groups(value)


@pytest.mark.parametrize('problem', ['duplicate_metadata', 'missing_metadata', 'blank_label'])
def test_contradictory_or_missing_group_metadata_is_never_locally_resolved(problem):
    value = answer()
    if problem == 'duplicate_metadata': value['groups'].append(deepcopy(value['groups'][0]))
    elif problem == 'missing_metadata': value['groups'].pop()
    else: value['groups'][0]['canonical_label'] = ' '
    with pytest.raises(IncompleteResponseError):
        packet().groups(value)


def test_unused_group_metadata_is_safely_pruned():
    value = answer()
    value['groups'].append({'group_id': 3, 'joins_existing_registry_node': False, 'canonical_label': 'Unused Q', 'explanation': ''})
    result = packet().groups(value)
    assert len(result) == 2


def test_provisional_identity_must_stay_in_the_same_model_selected_group():
    request = packet()
    request.assignments = {'E1': 'N1'}
    with pytest.raises(IncompleteResponseError, match='E1 must retain N1'):
        request.groups(answer())


@pytest.mark.parametrize('group_index,declared', [(0, True), (1, False)])
def test_declared_registry_join_cannot_disagree_with_actual_assignments(group_index, declared):
    value = answer()
    value['groups'][group_index]['joins_existing_registry_node'] = declared
    original = deepcopy(value)
    with pytest.raises(IncompleteResponseError, match='conflicts with registry_assignments'):
        packet().groups(value)
    assert value == original  # The checker must never repair the scientific selection locally.


def test_prior_assembly_version_does_not_seed_new_identity_decisions(tmp_path):
    saved_claim(tmp_path, 'Document A', 'Crop C', 'contains', 'Compound T')
    first = assemble_pathways(tmp_path, Aligner(), configuration())
    path = tmp_path / '.pathways/snapshot.json'
    first.update(version=1, input_signature='prior-identity-policy')
    path.write_text(json.dumps(first), encoding='utf-8')
    checkpoint = tmp_path / '.pathways/checkpoint.json'
    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    state.update(input_signature='prior-identity-policy', request_protocol='keyed-identity-assignments-v2')
    checkpoint.write_text(json.dumps(state), encoding='utf-8')
    client = Aligner()
    rebuilt = assemble_pathways(tmp_path, client, configuration())
    assert len(client.calls) == 2
    from backend.stages.pathway_assembly.corpus import ASSEMBLY_VERSION
    assert rebuilt['version'] == ASSEMBLY_VERSION
    assert not rebuilt['candidate_selection']['reused_endpoint_count']


def test_existing_nodes_can_be_left_unchanged_or_explicitly_merged_without_incoming_members():
    request = packet()
    request.node_ids['N2'] = 'another-stored-node'
    from backend.stages.pathway_assembly.contract import response_schema
    request.schema = response_schema(request.endpoint_ids, request.node_ids)
    value = answer()
    value['endpoint_assignments'] = {'E1': 1, 'E2': 1}
    value['registry_assignments'] = {'N1': 2, 'N2': 2}
    groups = request.groups(value)
    assert groups[1]['endpoint_ids'] == []
    assert groups[1]['existing_node_ids'] == ['stored-long-node', 'another-stored-node']
    value['registry_assignments'] = {'N1': None, 'N2': None}
    value['groups'] = value['groups'][:1]
    assert request.groups(value)[0]['existing_node_ids'] == []


def test_duplicate_json_keys_are_rejected_by_the_provider_instead_of_selecting_a_winner(monkeypatch):
    from backend.providers.structured_output import DuplicateJSONPropertyError, parse_json_object
    with pytest.raises(DuplicateJSONPropertyError, match='E1'):
        parse_json_object('{"endpoint_assignments":{"E1":1,"E1":2},"registry_assignments":{},"groups":[]}')


def test_original_overlap_failure_reports_the_exact_conflicting_ids():
    groups = {'groups': [dict(canonical_label='Class Q', endpoint_ids=['E1', 'E2'], existing_node_ids=[], explanation=''),
                         dict(canonical_label='Member R', endpoint_ids=['E2'], existing_node_ids=[], explanation='')]}
    with pytest.raises(IncompleteResponseError, match='repeated endpoint IDs: E2'):
        check_groups(groups, [{'endpoint_id': 'E1'}, {'endpoint_id': 'E2'}], set())


def test_a_failed_assignment_regenerates_with_the_same_model_and_original_evidence(monkeypatch):
    request = packet()
    invalid = answer()
    invalid['groups'].pop()
    responses = iter([{'content': json.dumps(value), 'finish_reason': 'stop'} for value in (invalid, answer())])
    client = OpenCodeClient(replace(Settings(), thinking={'pathway_assembly': False}), task='pathway_assembly')
    calls = []
    def chat(model, system, user, schema, limit):
        calls.append((model, user, schema))
        return next(responses)
    monkeypatch.setattr(client._inference, 'chat', chat)
    result = client.chat_json(SYSTEM, request.user, json_schema=request.schema, response_check=request.groups, max_tokens=6000)
    assert request.groups(result)
    assert len(calls) == 2
    assert all(c[0] == Settings().pathway_assembly_model for c in calls)
    assert calls[0][1:] == calls[1][1:]


def test_old_checkpoint_with_stable_ids_resumes_using_request_local_ids(tmp_path):
    saved_claim(tmp_path, 'Document A', 'Crop C', 'contains', 'Compound T')
    with pytest.raises(LLMServiceError):
        assemble_pathways(tmp_path, Aligner(fail_at=2), configuration())
    path = tmp_path / '.pathways/checkpoint.json'
    state = json.loads(path.read_text())
    state.pop('request_protocol', None)  # Compatible checkpoint from the old wire format.
    path.write_text(json.dumps(state), encoding='utf-8')
    client = Aligner()
    snapshot = assemble_pathways(tmp_path, client, configuration())
    assert len(client.calls) == 1 and snapshot['counts']['endpoints'] == 2
    assert client.calls[0]['incoming_endpoints'][0]['endpoint_id'] == 'E1'
    assert all(not key.startswith(('E1', 'N1')) for key in (n['id'] for n in snapshot['graph']['nodes']))


def test_cannot_link_constraint_prevents_same_edge_subject_object_merge():
    batch = [
        {'endpoint_id': 'edge-test:subject', 'edge_id': 'edge-test', 'role': 'subject', 'name': 'Practice P'},
        {'endpoint_id': 'edge-test:object', 'edge_id': 'edge-test', 'role': 'object', 'name': 'Disease D'},
    ]
    pkt = IdentityPacket(batch, {}, {})
    # Attempting to assign both E1 and E2 to group 1
    invalid_assignment = {
        'endpoint_assignments': {'E1': 1, 'E2': 1},
        'registry_assignments': {},
        'groups': [{'group_id': 1, 'joins_existing_registry_node': False, 'canonical_label': 'Combined', 'explanation': 'test'}]
    }
    with pytest.raises(IncompleteResponseError, match='Cannot merge both subject and object of the same claim edge'):
        pkt.groups(invalid_assignment)
