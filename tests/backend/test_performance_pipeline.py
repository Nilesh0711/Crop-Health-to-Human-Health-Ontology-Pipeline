"""Verify bounded overlap, stable evidence selection and explicit activity ownership."""
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from dataclasses import replace
import pytest
from backend.config.settings import Settings
from backend.pipelines.external import prefetch_evidence
from backend.shared.activity import TaggedMessage, display_log


def test_prefetch_prepares_next_claim_during_current_judgement():
    ready = Event()
    calls = []
    def prepare(value):
        calls.append(value)
        if value == 2:
            ready.set()
        return {'claim': value}
    with prefetch_evidence([1, 2, 3], prepare) as iterator:
        claim, future = next(iterator)
        assert future.result(timeout=2) == {'claim': 1}
        assert ready.wait(2)  # Current cloud judge could be waiting here.
        assert calls == [1, 2]  # Never prepare an unbounded claim queue.
        assert next(iterator)[1].result(timeout=2) == {'claim': 2}
        assert next(iterator)[1].result(timeout=2) == {'claim': 3}


def test_prefetch_errors_belong_to_the_affected_claim_and_do_not_hide_current_work():
    def prepare(value):
        if value == 2:
            raise ValueError('paper preparation failed')
        return value
    with prefetch_evidence([1, 2], prepare) as iterator:
        assert next(iterator)[1].result(timeout=2) == 1
        with pytest.raises(ValueError, match='preparation failed'):
            next(iterator)[1].result(timeout=2)


def test_prefetch_can_be_disabled():
    calls = []
    with prefetch_evidence([1, 2], lambda c: calls.append(c) or c, enabled=False) as iterator:
        assert next(iterator)[1].result() == 1
        assert calls == [1]
        assert next(iterator)[1].result() == 2


def test_fast_paper_is_ranked_while_slow_paper_is_still_preparing(monkeypatch):
    module = importlib.import_module('backend.stages.literature_retrieval.workflow')
    ranked_fast = Event()
    slow_started = Event()
    records = [{'id': str(i), 'source':'MED', 'title':f'paper {i}',
                'abstractText':f'material{i} ' + 'Microbial activity was measured under controlled conditions. ' * 3} for i in range(2)]
    monkeypatch.setattr(module, 'search', lambda *args: records)
    monkeypatch.setattr(module, 'search_openalex', lambda *args: [])
    monkeypatch.setattr(module, '_hybrid_order', lambda h,c,e,r: [(1-i/10, entry) for i,entry in enumerate(e)])
    def chunks(paragraphs):
        if paragraphs[0].startswith('material0'):
            slow_started.set()
            assert ranked_fast.wait(2), 'Ranking incorrectly waited for the entire fetch wave'
        else:
            assert slow_started.wait(2)
        return [{'text':paragraphs[0]}]
    monkeypatch.setattr(module, 'make_chunks', chunks)
    def select(query, chunks, key, count, **kwargs):
        if chunks[0]['text'].startswith('material1'):
            ranked_fast.set()
        return [{'chunk_id':key + ':0', 'text':chunks[0]['text']}]
    ranker = SimpleNamespace(select=select)
    events = []
    result = module.retrieve_round({'candidate_id':'c', 'subject':'lead', 'relation':'reduces',
                                   'object':'microbial activity'}, None, ranker, replace(Settings(), retrieval_workers=2),
                                  2, 4, query_translation={'query':'lead microbial activity'}, progress=events.append)
    assert [d['title'] for d in result['evidence_documents']] == ['paper 0', 'paper 1']
    assert len(result['paper_preparation_timings']) == 2
    assert {'article_ranking', 'article_fetch', 'chunk_preparation'} <= {e.stage for e in events}
    assert all(e.candidate_id == 'c' for e in events)
    assert all('ranking_seconds' in row for row in result['paper_preparation_timings'])


def test_activity_explicit_stage_beats_message_heuristics_and_old_logs_are_not_rewritten():
    original = {'message':'External validation', 'stage':'article_fetch', 'level':'info'}
    assert display_log(original)['stage'] == 'article_fetch'
    old = {'message':'Ranked 4 passages: example', 'level':'info'}
    assert display_log(old)['stage'] == 'chunk_preparation'
    assert 'stage' not in old
    assert TaggedMessage('papers', 'external').stage == 'external_validation'


@pytest.mark.parametrize('name', ['embedding_batch_size', 'cross_encoder_batch_size'])
def test_invalid_batch_sizes_fail_before_loading_models(name):
    with pytest.raises(ValueError, match='batch sizes'):
        replace(Settings(), **{name:0})


def test_prepared_packet_cannot_be_attached_to_another_claim():
    from backend.stages.external_validation.workflow import validate_candidate
    with pytest.raises(ValueError, match='does not match'):
        validate_candidate({'candidate_id':'wanted'}, None, None, Settings(),
                           prepared_evidence={'prepared_for_candidate_id':'different', 'evidence_documents':[]})


def test_configured_batches_reach_local_model_calls():
    from backend.stages.evidence_ranking.passages import SemanticReranker
    settings = replace(Settings(), embedding_batch_size=7, cross_encoder_batch_size=3)
    ranker = SemanticReranker(settings)
    calls = []
    class Dense:
        def encode(self, values, **kwargs):
            calls.append(kwargs['batch_size'])
            return []
    ranker._encode('sentence-transformers', Dense(), ['example'])
    class Cross:
        def predict(self, pairs, **kwargs):
            calls.append(kwargs['batch_size'])
            return [0.5] * len(pairs)
    ranker._cross_runtime = ('sentence-transformers', Cross())
    assert ranker.cross_scores('claim', ['passage']) == [0.5]
    assert calls == [7, 3]
