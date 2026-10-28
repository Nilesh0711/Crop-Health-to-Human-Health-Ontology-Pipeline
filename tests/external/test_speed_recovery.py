"""Evidence-only Pro correction and paper-ready orchestration."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import pytest

from backend.providers.inference import InferenceHTTP
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.stages.trust_graph.workflow import assess_group
from tests.external.test_trust_graph import config, fixtures
from tests.external.test_paper_synthesis import synthesis


@pytest.mark.parametrize('patch_kind', ['valid', 'invented', 'changed_decision'])
def test_pro_repairs_only_one_claims_proof_with_one_smaller_request(tmp_path, monkeypatch, patch_kind):
    claims, packets, answers = fixtures(2, 1)
    paper = {'results': {c['candidate_id']: answers[(c['candidate_id'], 'paper-0')] for c in claims}}
    # The provider's row order differs from input order. A correction must use
    # the matching identity, not the position in the input claims list.
    final = {'results': [{'candidate_id': c['candidate_id'], **synthesis(packets[c['candidate_id']]['evidence_documents'])}
                         for c in reversed(claims)], 'pathway_connections': []}
    good = deepcopy(final)
    final['results'][0]['semantic_assessment']['role_evidence'][0]['entity_mention'] = 'invented phrase'
    patch = {'role_evidence': good['results'][0]['semantic_assessment']['role_evidence']}
    if patch_kind == 'invented':
        patch = {'role_evidence': final['results'][0]['semantic_assessment']['role_evidence']}
    elif patch_kind == 'changed_decision':
        patch['paper_dispositions'] = []
    replies, calls, metrics = iter([paper, final, patch]), [], []
    def receive(self, method, url, **kwargs):
        calls.append(deepcopy(kwargs['json']))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(next(replies))}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 100}}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(config(), task='external', on_request=metrics.append, response_dir=tmp_path / 'responses')
    if patch_kind == 'changed_decision':
        with pytest.raises(LLMServiceError):
            assess_group(claims, packets, client, config(), tmp_path / 'assessments')
    else:
        results, _, _ = assess_group(claims, packets, client, config(), tmp_path / 'assessments')
        if patch_kind == 'valid':
            assert all(r['trust']['assessment_complete'] and r['trust']['score'] == 1 for r in results)
            accepted = json.loads(__import__('pathlib').Path(metrics[-1]['response_artifact']).read_text())['accepted_response']
            assert accepted == good
        else:
            held = next(r for r in results if r['candidate_id'] == 'claim-1')
            assert held['validation_status'] == 'external_support_needs_review'
            assert held['attempts'][-1]['joint_assessment']['semantic_assessment']['role_grounding_errors']
    assert len(calls) == 3  # One paper, one synthesis, ONE bounded correction.
    assert calls[1]['model'] == calls[2]['model'] == 'mimo-v2.6-pro'
    assert metrics[-1]['purpose'] == 'proof_correction'
    assert calls[2]['max_tokens'] == 4096
    repair_input = json.loads(calls[2]['messages'][1]['content'])
    assert len(calls[2]['messages'][1]['content']) < len(calls[1]['messages'][1]['content'])
    assert 'claims' not in repair_input and 'pathway_connections' not in repair_input
    assert 'role_evidence' not in repair_input['fixed_assessment']


def test_paper_is_judged_before_later_claim_packet_is_finished(tmp_path, monkeypatch):
    from backend.pipelines import trust
    from backend.shared.cache import RunCache
    from tests.external.test_trust_graph import GroupClient
    claims, packets, answers = fixtures(2, 1)
    paper_started, checked_visible = Event(), Event()
    original_chat = GroupClient.chat_json
    def chat(self, system, user, **options):
        if options['request_label'].startswith('grouped paper'):
            paper_started.set()
        return original_chat(self, system, user, **options)
    monkeypatch.setattr(GroupClient, 'chat_json', chat)
    def prepare(candidate, *args, **kwargs):
        cid = candidate['candidate_id']
        if cid == 'claim-1':
            assert paper_started.wait(3), 'A later packet must not block ready paper judgement'
            assert checked_visible.wait(3), 'Checked findings must checkpoint before preparation finishes'
        kwargs['on_document'](deepcopy(packets[cid]['evidence_documents'][0]))
        return deepcopy(packets[cid])
    save = trust.save_rows
    def observe(path, rows):
        save(path, rows)
        if path.name == 'validation_runs.jsonl' and any(r.get('attempts', [{}])[-1].get('paper_judgements') for r in rows):
            checked_visible.set()
    monkeypatch.setattr(trust, 'save_rows', observe)
    monkeypatch.setattr(trust, 'prepare_evidence', prepare)
    results = []
    settings = replace(config(), evidence_index_enabled=False)
    stats = trust.run_trust_assessment(claims, claims, {'graph': {'nodes': [], 'edges': []}}, tmp_path,
        GroupClient(answers), None, settings, 8, {}, RunCache(), results, lambda *a: None)
    assert len(results) == 2 and all(r['trust']['assessment_complete'] for r in results)
    assert stats['early_paper_calls'] == 2 and stats['group_timings'][0]['final_paper_calls'] == 0
