"""Lookahead overlaps retrieval with cloud judgement and survives interruption."""
from dataclasses import replace
from threading import Event

import pytest

from backend.pipelines import trust
from backend.shared.cache import RunCache
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope, check_cancelled
from backend.stages.external_validation.workflow import _signature, prepare_evidence
from tests.external.test_trust_graph import config, fixtures, GroupClient


def setup_case(tmp_path):
    claims, _, answers = fixtures(2, 0)
    settings = replace(config(), trust_batch_size=1, external_prefetch=True)
    client = GroupClient(answers)
    provisional = {'graph': {'nodes': [], 'edges': []}}
    def run(results, resume=False):
        return trust.run_trust_assessment(claims, claims, provisional, tmp_path, client, None, settings,
            8, {c['candidate_id']: {} for c in claims}, RunCache(), results, lambda *_: None, resume=resume)
    def packet(candidate, *_args, **_kwargs):
        return {'evidence_documents': [], 'retrieval_signature': _signature(candidate,
            replace(settings, thinking={**settings.task_thinking, 'external': True}), 8, settings.external_top_chunks)}
    return claims, settings, run, packet


def test_next_group_preparation_overlaps_current_judgement_with_one_retrieval_owner(tmp_path, monkeypatch):
    claims, settings, run, packet = setup_case(tmp_path)
    second_started, judgement_started = Event(), Event()
    actual_assess = trust.assess_group
    def prepare(candidate, *args, **kwargs):
        if candidate['candidate_id'] == claims[1]['candidate_id']:
            second_started.set()
            assert judgement_started.wait(3), 'Retrieval must overlap the current cloud assessment'
        return packet(candidate)
    def assess(group, *args, **kwargs):
        if group[0]['candidate_id'] == claims[0]['candidate_id']:
            assert second_started.wait(3), 'One group of lookahead must be started'
            judgement_started.set()
        return actual_assess(group, *args, **kwargs)
    monkeypatch.setattr(trust, 'prepare_evidence', prepare)
    monkeypatch.setattr(trust, 'assess_group', assess)
    results = []
    stats = run(results)
    assert len(results) == 2 and stats['groups'] == 2
    assert len(stats['group_timings']) == 2 and stats['provider_attempts_this_run'] is None
    assert len(list((tmp_path / 'trust_prepared').glob('*.json'))) == 2


def test_prefetched_selected_passages_survive_failure_and_resume_without_refetch(tmp_path, monkeypatch):
    claims, settings, run, packet = setup_case(tmp_path)
    second_saved = Event()
    original_save = trust.save_json
    def save(path, value):
        original_save(path, value)
        if path.parent.name == 'trust_prepared' and path.stem == claims[1]['candidate_id']:
            second_saved.set()
    monkeypatch.setattr(trust, 'save_json', save)
    monkeypatch.setattr(trust, 'prepare_evidence', packet)
    real_assess = trust.assess_group
    def fail(*_args, **_kwargs):
        assert second_saved.wait(3)
        raise RuntimeError('Synthetic judgement interruption')
    monkeypatch.setattr(trust, 'assess_group', fail)
    with pytest.raises(RuntimeError, match='Synthetic'):
        run([])
    monkeypatch.setattr(trust, 'assess_group', real_assess)
    seen = []
    def reuse(candidate, *args, **kwargs):
        seen.append(candidate['candidate_id'])
        assert kwargs['resume_record']['attempts'], 'Selected text must be restored from the prepared checkpoint'
        # The actual implementation takes the reuse branch: no network/ranker.
        return prepare_evidence(candidate, *args, **kwargs)
    monkeypatch.setattr(trust, 'prepare_evidence', reuse)
    results = []
    run(results, resume=True)
    assert len(seen) == len(results) == 2
    assert all(r['validation_status'] == 'retrieval_failed_or_no_open_access_evidence' for r in results)


def test_cancelled_lookahead_does_not_cancel_its_parent_or_continue_new_work():
    from backend.pipelines.external import prefetch_evidence
    from backend.shared.cancellation import current_token
    parent = CancellationToken()
    second_started = Event()
    pause = Event()
    def prepare(item):
        if item == 2:
            second_started.set()
            while not current_token().requested:
                pause.wait(0.01)
            check_cancelled()
        return item
    with cancellation_scope(parent):
        with pytest.raises(RuntimeError, match='Judge failed'):
            with prefetch_evidence([1, 2, 3], prepare, cancel_lookahead_on_exit=True) as packets:
                _, future = next(packets)
                assert future.result() == 1 and second_started.wait(3)
                raise RuntimeError('Judge failed')
        assert not parent.requested
        child = CancellationToken(parent=parent)
        parent.request()
        with pytest.raises(PipelineStopped):
            child.check()
