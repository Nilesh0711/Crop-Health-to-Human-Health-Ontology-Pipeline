"""Reuse identical source downloads without changing paper identity or evidence."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from backend.config.settings import Settings
from backend.shared.cache import RunCache
from backend.stages.literature_retrieval import workflow


def test_concurrent_cache_requests_share_one_computation_and_receive_isolated_values():
    cache = RunCache()
    barrier, release = Barrier(3, timeout=3), Event()
    calls = []
    def compute():
        calls.append(1)
        assert release.wait(3)
        return {'paragraphs': ['Exact source text']}
    def request():
        barrier.wait()
        return cache.get_or_compute('one-source', compute)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(request) for _ in range(3)]
        release.set()
        answers = [f.result(timeout=3) for f in futures]
    assert len(calls) == 1
    answers[0]['paragraphs'].append('local mutation')
    assert answers[1]['paragraphs'] == ['Exact source text']
    assert cache.get_or_compute('one-source', lambda: None) == {'paragraphs': ['Exact source text']}


def test_cache_failure_is_not_saved_and_waiting_job_can_be_cancelled():
    from backend.shared.cancellation import CancellationToken, cancellation_scope, PipelineStopped
    cache = RunCache()
    with pytest.raises(RuntimeError):
        cache.get_or_compute('failed', lambda: (_ for _ in ()).throw(RuntimeError('Unavailable')))
    assert cache.get_or_compute('failed', lambda: {'value': 'recovered'}) == {'value': 'recovered'}
    started, release = Event(), Event()
    def compute():
        started.set()
        assert release.wait(3)
        return {'text': 'source'}
    token = CancellationToken()
    def cancelled_waiter():
        with cancellation_scope(token):
            token.request()
            return cache.get_or_compute('waiting', lambda: pytest.fail('Duplicate fetch'))
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(cache.get_or_compute, 'waiting', compute)
        assert started.wait(3)
        try:
            with pytest.raises(PipelineStopped):
                executor.submit(cancelled_waiter).result(timeout=3)
        finally:
            release.set()
        assert owner.result(timeout=3) == {'text': 'source'}


def test_identical_fulltext_locator_is_reused_across_different_metadata_abstracts(monkeypatch):
    calls, cache = [], RunCache()
    paragraphs = [{'text': 'Exact finding with conditions.', 'section_heading': 'Results'}]
    def fetch(source, identifier, settings):
        calls.append((source, identifier))
        return '<article>original</article>'
    monkeypatch.setattr(workflow, 'fetch_fulltext_xml', fetch)
    monkeypatch.setattr(workflow, 'extract_paragraphs', lambda xml, **kw: paragraphs)
    first = workflow._paper_material({'abstractText': 'First abstract'}, [('PMC', 'paper-1')], Settings(), cache)
    second = workflow._paper_material({'abstractText': 'Different indexed abstract'}, [('PMC', 'paper-1')], Settings(), cache)
    assert first == second == (paragraphs, 'full_text') and calls == [('PMC', 'paper-1')]


def test_unavailable_fulltext_is_reused_only_within_run_and_keeps_each_records_abstract(monkeypatch):
    calls, cache = [], RunCache()
    def fetch(*args):
        calls.append(1)
        return None
    monkeypatch.setattr(workflow, 'fetch_fulltext_xml', fetch)
    for text in ('A' * 90, 'B' * 90):
        assert workflow._paper_material({'abstractText': text}, [('PMC', 'missing')], Settings(), cache) == ([text], 'abstract')
    assert len(calls) == 1
    workflow._paper_material({}, [('PMC', 'missing')], Settings(), RunCache())
    assert len(calls) == 2
