"""Global alias alignment, preserved edges, resumable registry scans and API reads."""
import importlib
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.providers.llm import LLMServiceError
from backend.providers.structured_output import IncompleteResponseError
from backend.shared.artifacts import save_rows
from backend.stages.pathway_assembly.assembly import assemble_pathways, check_groups, read_assembly
from backend.stages.pathway_assembly.corpus import collect_corpus
from backend.stages.pathway_assembly.paths import assembled_paths


def saved_claim(root, paper, subject, relation, obj, *, condition='', source_status='entailed'):
    folder = root / paper
    folder.mkdir(parents=True, exist_ok=True)
    claim = dict(candidate_id='same-id-in-each-document', subject=subject, relation=relation, object=obj,
                 subject_type='Crop', object_type='Contaminant', certainty='conditional', schema_error=None,
                 source_paper_title=paper, source_document_id=paper, source_page=2,
                 source_evidence_span=f'{subject} {relation} {obj}.',
                 source_local_context=f'{subject} {relation} {obj}. The crop in this paragraph is wheat.',
                 qualifiers={'condition':condition}, source_validation={'decision':source_status},
                 semantic_validation={'decision':'not_run'})
    save_rows(folder / 'candidates.jsonl', [claim])
    save_rows(folder / 'validation_runs.jsonl', [dict(candidate_id=claim['candidate_id'],
              validation_status='world_independently_corroborated_pending_review',
              support_summary={'independent_groups': 2}, attempts=[{
                  'joint_assessment': {'semantic_assessment': {'in_scope': True}}, 'decisions':[
                  {'evidence_document_id': 'support-a', 'decision': 'supported', 'evidence_level': 'independent_external'},
                  {'evidence_document_id': 'support-b', 'decision': 'supported', 'evidence_level': 'independent_external'}]}])])
    return folder, claim


class Aligner:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def chat_json(self, system, user, **kwargs):
        data = json.loads(user)
        self.calls.append(data)
        if len(self.calls) == self.fail_at:
            raise LLMServiceError('Simulated assembly provider error')
        groups = {}
        def canonical(name):
            return 'wheat grains' if name.lower() in {'wheat grains', 'these infected grains', 'infected grains'} else name.lower()
        for endpoint in data['incoming_endpoints']:
            label = canonical(endpoint['name'])
            group = groups.setdefault(label, dict(canonical_label=label, endpoint_ids=[], existing_node_ids=[],
                                                   explanation='Equivalent entity in explicit local context.'))
            group['endpoint_ids'].append(endpoint['endpoint_id'])
            for node in data['registry_nodes']:
                if canonical(node['canonical_label']) == label or node['node_id'] == endpoint.get('provisional_node_id'):
                    if node['node_id'] not in group['existing_node_ids']:
                        group['existing_node_ids'].append(node['node_id'])
        answer = {'groups':list(groups.values())}
        kwargs['response_check'](answer)
        return answer


def configuration(**kwargs):
    return replace(Settings(), pathway_assembly_batch_size=1, **kwargs)


def test_connected_validated_claims_across_papers_keep_edges_and_conditions(tmp_path):
    saved_claim(tmp_path, 'paper A', 'Fusarium head blight', 'affects', 'wheat grains')
    saved_claim(tmp_path, 'paper B', 'These infected grains', 'has_contaminant', 'mycotoxins',
                condition='Fusarium head blight infection')
    before = {p:p.read_bytes() for p in tmp_path.glob('*/*.jsonl')}
    client = Aligner()
    snapshot = assemble_pathways(tmp_path, client, configuration())
    assert snapshot['counts'] == dict(claims=2, endpoints=4, entities=3, papers=2)
    edges = snapshot['graph']['edges']
    assert edges[0]['target'] == edges[1]['source']
    assert len({e['id'] for e in edges}) == 2  # Namespace equal candidate IDs by document.
    assert edges[1]['qualifiers']['condition'] == 'Fusarium head blight infection'
    assert edges[1]['source_status'] == 'entailed'
    assert all(e['status'] == 'corroborated' for e in edges)
    assert len(assembled_paths(snapshot['graph'])['paths'][0]) == 2
    assert not snapshot['causal_chain_validated']
    assert before == {p:p.read_bytes() for p in tmp_path.glob('*/*.jsonl')}
    assert not any(e['subject'] == 'Fusarium head blight' and e['object'] == 'mycotoxins' for e in edges)


def test_matching_snapshot_and_external_status_only_change_do_not_repeat_alignment(tmp_path):
    folder, _ = saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    client = Aligner()
    first = assemble_pathways(tmp_path, client, configuration())
    calls = len(client.calls)
    assert assemble_pathways(tmp_path, client, configuration()) == first
    save_rows(folder / 'validation_runs.jsonl', [dict(candidate_id='same-id-in-each-document',
              validation_status='world_independent_supported_pending_corroboration', attempts=[{
                  'joint_assessment': {'semantic_assessment': {'in_scope': True}},
                  'decisions':[{'evidence_document_id':'paper', 'decision':'supported'}]}])])
    changed = assemble_pathways(tmp_path, client, configuration())
    assert changed['graph']['edges'][0]['external_status'] == 'world_independent_supported_pending_corroboration'
    assert len(client.calls) == calls


def test_new_paper_compares_with_global_registry_without_regenerating_old_occurrences(tmp_path):
    saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    client = Aligner()
    first = assemble_pathways(tmp_path, client, configuration())
    old_ids = {r['endpoint_id'] for r in first['endpoints']}
    old_count = len(client.calls)
    saved_claim(tmp_path, 'B', 'These infected grains', 'has_contaminant', 'cadmium')
    second = assemble_pathways(tmp_path, client, configuration())
    assert second['counts']['entities'] == 3
    incoming = {r['endpoint_id'] for call in client.calls[old_count:] for r in call['incoming_endpoints']}
    assert not incoming.intersection(old_ids)
    assert len(incoming) == 2
    assert any(call['registry_nodes'] for call in client.calls[old_count:])


def test_registry_is_sharded_without_dropping_global_occurrences(tmp_path):
    for n in range(7):
        saved_claim(tmp_path, str(n), f'crop {n}', 'has_contaminant', f'toxin {n}')
    client = Aligner()
    snapshot = assemble_pathways(tmp_path, client, configuration(pathway_assembly_input_characters=6500))
    assert snapshot['counts']['endpoints'] == 14
    assert len(client.calls) > 14  # Multiple global registry shards for later batches.
    all_ids = [i for node in snapshot['graph']['nodes'] for i in node['endpoint_ids']]
    assert len(all_ids) == len(set(all_ids)) == 14
    assert all(len(json.dumps(call, ensure_ascii=False)) <= 6500 for call in client.calls)


def test_provider_failure_does_not_publish_partial_snapshot_and_resume_keeps_completed_calls(tmp_path):
    saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    first = Aligner(fail_at=2)
    with pytest.raises(LLMServiceError):
        assemble_pathways(tmp_path, first, configuration())
    assert not (tmp_path / '.pathways/snapshot.json').exists()
    assert read_assembly(tmp_path, Settings().pathway_assembly_model)['status'] == 'failed'
    retry = Aligner()
    snapshot = assemble_pathways(tmp_path, retry, configuration())
    assert snapshot['counts']['endpoints'] == 2
    assert len(retry.calls) == 1


@pytest.mark.parametrize('problem', ['invented', 'missing', 'duplicate', 'unknown_node'])
def test_model_cannot_invent_or_omit_endpoint_or_node_ids(problem):
    answer = {'groups':[dict(canonical_label='wheat', endpoint_ids=['one'], existing_node_ids=[], explanation='same') ]}
    if problem == 'invented':
        answer['groups'][0]['endpoint_ids'] = ['invented']
    elif problem == 'missing':
        answer['groups'] = []
    elif problem == 'duplicate':
        answer['groups'].append(deepcopy(answer['groups'][0]))
    else:
        answer['groups'][0]['existing_node_ids'] = ['invented']
    with pytest.raises(IncompleteResponseError):
        check_groups(answer, [{'endpoint_id':'one'}], set())


def test_input_overflow_is_visible_and_no_partial_snapshot_replaces_good_work(tmp_path):
    saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    client = Aligner()
    with pytest.raises(ValueError, match='input budget'):
        assemble_pathways(tmp_path, client, replace(configuration(), pathway_assembly_input_characters=1000,
                                                  pathway_assembly_batch_size=24))
    assert not client.calls


def test_read_and_pagination_are_read_only_and_mark_stale_corpus(tmp_path, monkeypatch):
    saved_claim(tmp_path, 'A', 'disease', 'affects', 'wheat grains')
    saved_claim(tmp_path, 'B', 'These infected grains', 'has_contaminant', 'mycotoxins')
    assemble_pathways(tmp_path, Aligner(), configuration())
    module = importlib.import_module('backend.api.app')
    monkeypatch.setattr(module, 'ONTOLOGY_DIR', tmp_path)
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    data = TestClient(module.app).get('/api/pathways?limit=1').json()
    assert len(data['paths']) == 1 and not data['stale']
    assert before == {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    saved_claim(tmp_path, 'C', 'mycotoxins', 'affects', 'human exposure')
    assert TestClient(module.app).get('/api/pathways').json()['stale']


def test_assembly_action_queues_a_job_without_clearing_claims_or_starting_models(tmp_path, monkeypatch):
    module = importlib.import_module('backend.api.app')
    folder, _ = saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    monkeypatch.setattr(module, '_jobs', {})
    monkeypatch.setattr(module, '_pdf_path', lambda _:tmp_path / 'A.pdf')
    monkeypatch.setattr(module, '_output_dir', lambda _:folder)
    thread = Mock()
    monkeypatch.setattr(module.threading, 'Thread', thread)
    before = (folder / 'candidates.jsonl').read_bytes()
    result = TestClient(module.app).post('/api/pdfs/A/assemble-pathways')
    assert result.status_code == 202 and result.json()['action'] == 'assemble_pathways'
    assert (folder / 'candidates.jsonl').read_bytes() == before
    thread.return_value.start.assert_called_once()


def test_global_assembly_worker_uses_saved_claim_workflow_not_full_pipeline(tmp_path, monkeypatch):
    module = importlib.import_module('backend.api.app')
    folder, _ = saved_claim(tmp_path, 'A', 'wheat grains', 'has_contaminant', 'mycotoxins')
    monkeypatch.setattr(module, 'ONTOLOGY_DIR', tmp_path)
    monkeypatch.setattr(module, '_output_dir', lambda _:folder)
    monkeypatch.setattr(module, '_jobs', {'job':dict(id='job', pdf_id='A', pdf_path=str(tmp_path/'A.pdf'),
                         status='queued', action='assemble_pathways', logs=[])})
    full, repair, assembly = Mock(), Mock(), Mock(return_value={'run_status':'completed'})
    monkeypatch.setattr(module, 'run_pipeline', full)
    monkeypatch.setattr(module, 'run_claim_action', repair)
    monkeypatch.setattr(module, 'assemble_saved_pathways', assembly)
    module._worker('job', module.RunOptions())
    assert module._jobs['job']['status'] == 'completed'
    full.assert_not_called()
    repair.assert_not_called()
    assert assembly.call_args.args[:2] == (tmp_path, folder)


def test_large_shared_node_keeps_all_occurrence_contexts_across_registry_fragments(tmp_path):
    for n in range(12):
        saved_claim(tmp_path, f'paper-{n:02}', 'wheat grains', 'has_contaminant', 'mycotoxins')
    client = Aligner()
    snapshot = assemble_pathways(tmp_path, client, configuration(pathway_assembly_input_characters=6500))
    assert snapshot['counts']['entities'] == 2
    last_id = snapshot['endpoints'][-1]['endpoint_id']
    scanned = {context['paper'] for call in client.calls
               if any(e['endpoint_id'] == last_id for e in call['incoming_endpoints'])
               for node in call['registry_nodes'] for context in node['identity_context']}
    assert {f'paper-{n:02}' for n in range(11)} <= scanned
    assert sum(len(node['endpoint_ids']) for node in snapshot['graph']['nodes']) == 24


def test_pathway_model_changes_do_not_invalidate_existing_external_judgments():
    from dataclasses import asdict
    from types import SimpleNamespace
    from backend.stages.external_validation.workflow import validation_signature
    settings = Settings()
    old = SimpleNamespace(**asdict(settings), task_thinking=settings.task_thinking, task_models={k:v for k,v in settings.task_models.items()
                                                        if k != 'pathway_assembly'})
    claim = {'candidate_id':'kept', 'subject':'wheat', 'relation':'has_contaminant', 'object':'cadmium'}
    assert validation_signature(claim, settings) == validation_signature(claim, old)
    assert validation_signature(claim, replace(settings, pathway_assembly_model='opencode-go/mimo-v2.6-pro')) == validation_signature(claim, old)
