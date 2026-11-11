"""Grounding concurrency, cancellation, provenance and durable partial progress."""
import json
import threading
from dataclasses import replace

import pytest

from backend.config.settings import Settings
from backend.config.pipeline import active_task_models
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.inference import InferenceError
from backend.shared.cancellation import CancellationToken, cancellation_scope, current_token, PipelineStopped
from backend.shared.storage import read_jsonl
from backend.stages.ontology_grounding import grounder
from backend.stages.ontology_grounding.scheduling import run_packets
from tests.backend.test_ontology_grounding import claim, Grounder, reference  # noqa: F401


def test_deepseek_is_assigned_to_every_default_stage_and_parallelism_is_bounded():
    settings = Settings()
    assert set(settings.task_models.values()) == {'opencode-go/deepseek-v4.1-flash'}
    assert set(active_task_models(settings).values()) == {'opencode-go/deepseek-v4.1-flash'}
    assert settings.ontology_grounding_workers == 5
    assert settings.extraction_concurrency == 2
    for workers in [0, 9]:
        with pytest.raises(ValueError, match='ONTOLOGY_GROUNDING_WORKERS'):
            replace(settings, ontology_grounding_workers=workers)


def test_parallel_grounding_saves_out_of_order_results_with_their_own_request_metrics(tmp_path, reference, monkeypatch):
    barrier, saved_second = threading.Barrier(5), threading.Event()
    lock = threading.Lock()
    active = peak = 0
    observed_models, published = [], []
    original_save = grounder.save_rows
    def save(path, rows):
        original_save(path, rows)
        published.append([r['candidate_id'] for r in rows])
        if any(r['candidate_id'] == 'b' for r in rows):
            saved_second.set()
    monkeypatch.setattr(grounder, 'save_rows', save)
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly')
    def chat(model, system, user, schema, output_limit):
        nonlocal active, peak
        payload = json.loads(user)
        key = payload['claims']['C1']['qualifiers']['condition']
        with lock:
            active += 1; peak = max(peak, active); observed_models.append(model)
        try:
            barrier.wait(timeout=5)
            if key == 'a':
                assert saved_second.wait(5), 'The ready second result was not checkpointed'
            valid = Grounder().chat_json('', user, response_check=lambda _: None)
            return {'content': json.dumps(valid), 'finish_reason': 'stop', 'usage': {},
                    'response_format': 'json_object', 'provider_request_id': f'provider-{key}'}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(client._inference, 'chat', chat)
    rows = grounder.ground_claims([claim(k, qualifiers={'condition': k}) for k in 'abcde'],
                                tmp_path, reference, client, batch_size=1)
    assert peak == 5 and set(observed_models) == {'opencode-go/deepseek-v4.1-flash'}
    assert 'a' not in published[0]
    assert [r['candidate_id'] for r in rows] == list('abcde')
    assert [r['provider_request_id'] for r in rows] == [f'provider-{k}' for k in 'abcde']
    assert len(read_jsonl(tmp_path / 'ontology_grounding.jsonl')) == 5


def test_rate_limit_stops_new_batches_and_preserves_completed_checkpoints(tmp_path, reference, monkeypatch):
    saved_first, second_started = threading.Event(), threading.Event()
    original_save = grounder.save_rows
    def save(path, rows):
        original_save(path, rows)
        if rows:
            saved_first.set()
    monkeypatch.setattr(grounder, 'save_rows', save)
    calls = []
    client = OpenCodeClient(Settings(ontology_grounding_output_format='json', ontology_grounding_workers=2), task='pathway_assembly')
    def chat(model, system, user, schema, output_limit):
        payload = json.loads(user)
        key = payload['claims']['C1']['qualifiers']['condition']
        calls.append(key)
        if key == 'b':
            second_started.set()
            assert saved_first.wait(5)
            raise InferenceError('OpenCode inference HTTP 429: Rate or usage limit reached')
        if key == 'a':
            assert second_started.wait(5)
            valid = Grounder().chat_json('', user, response_check=lambda _: None)
            return {'content': json.dumps(valid), 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}
        # At most one replacement may already be in flight while b returns 429.
        while True:
            current_token().check()
            threading.Event().wait(.01)
    monkeypatch.setattr(client._inference, 'chat', chat)
    claims = [claim(k, qualifiers={'condition': k}) for k in 'abcde']
    with pytest.raises(LLMServiceError, match='429'):
        grounder.ground_claims(claims, tmp_path, reference, client, batch_size=1)
    assert 'd' not in calls and 'e' not in calls
    assert [r['candidate_id'] for r in read_jsonl(tmp_path / 'ontology_grounding.jsonl')] == ['a']
    assert client.model == 'opencode-go/deepseek-v4.1-flash'
    resumed = OpenCodeClient(Settings(ontology_grounding_output_format='json'), task='pathway_assembly')
    remaining = []
    def finish(model, system, user, schema, output_limit):
        remaining.append(json.loads(user)['claims']['C1']['qualifiers']['condition'])
        valid = Grounder().chat_json('', user, response_check=lambda _: None)
        return {'content': json.dumps(valid), 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}
    monkeypatch.setattr(resumed._inference, 'chat', finish)
    rows = grounder.ground_claims(claims, tmp_path, reference, resumed, batch_size=1)
    assert len(rows) == 5 and set(remaining) == set('bcde')


def test_stop_propagates_to_inflight_workers_and_prevents_further_dispatch():
    parent, entered = CancellationToken(), threading.Barrier(2)
    calls, published = [], []
    def assess(packet):
        calls.append(packet['number'])
        assert current_token() is not None
        entered.wait(timeout=5)
        parent.request()
        current_token().check()
    with cancellation_scope(parent), pytest.raises(PipelineStopped):
        run_packets([{'number': n} for n in range(5)], assess, published.append, 2)
    assert sorted(calls) == [0, 1] and not published
