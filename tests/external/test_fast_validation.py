"""Persistent evidence, warm models, bounded source waits and early judgement."""
from copy import deepcopy
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import json
import sqlite3
import time

import pytest

from backend.shared.cache import RunCache
from backend.shared.evidence_index import EvidenceIndex, EvidenceIndexError, POLICY
from backend.shared.cancellation import current_token, check_cancelled
from backend.stages.literature_retrieval.scheduling import collect_searches
from backend.stages.literature_retrieval.workflow import _paper_material
from backend.stages.trust_graph.ready import ReadyPaperJudge
from backend.stages.trust_graph.workflow import assess_group
from tests.external.test_trust_graph import fixtures, GroupClient, config


def test_source_text_survives_new_run_cache_and_expired_material_is_refetched(tmp_path, monkeypatch):
    from backend.stages.literature_retrieval import workflow
    calls = []
    monkeypatch.setattr(workflow, 'fetch_fulltext_xml', lambda *a: calls.append(1) or '<article/>')
    paragraphs = [{'text': 'Exact result. Condition C applies.', 'section_heading': 'Results'}]
    monkeypatch.setattr(workflow, 'extract_paragraphs', lambda *a, **kw: deepcopy(paragraphs))
    for _ in range(2):
        cache = RunCache(persistent=EvidenceIndex(tmp_path))
        assert _paper_material({}, [('PMC', 'fixed-id')], config(), cache) == (paragraphs, 'full_text')
    assert calls == [1]
    with sqlite3.connect(tmp_path / 'evidence.sqlite3') as db:
        db.execute('UPDATE entries SET saved=0')
    _paper_material({}, [('PMC', 'fixed-id')], config(), RunCache(persistent=EvidenceIndex(tmp_path)))
    assert calls == [1, 1]


def test_index_hash_size_limits_and_metadata_citation_isolation(tmp_path):
    index = EvidenceIndex(tmp_path)
    index.catalogue('doi:10.1234/test', {'doi': '10.1234/test', 'title': 'Compound T affects Process P',
        'abstractText': 'Exact source abstract', '_source_reference_ids': ['claim-specific'], '_retrieval_route': 'source_citation'})
    record = index.search_papers('Compound T Process P')[0]
    assert record['doi'] == '10.1234/test' and record['_retrieval_route'] == 'shared_evidence_index'
    assert '_source_reference_ids' not in record
    index.put('proof', {'text': 'Original source wording'})
    with sqlite3.connect(index.path) as db:
        db.execute('UPDATE entries SET digest=? WHERE key=?', ('wrong', POLICY + ':proof'))
    with pytest.raises(EvidenceIndexError, match='content hash'):
        index.get('proof')
    small = EvidenceIndex(tmp_path / 'small', max_bytes=70)
    for n in range(10):
        small.put(str(n), {'value': n})
    assert small.stats()['compressed_bytes'] <= 70 and small.get('9') == {'value': 9}


def test_persistent_embeddings_are_reused_but_new_queries_and_models_recompute(tmp_path, monkeypatch):
    import numpy as np
    from backend.stages.evidence_ranking.passages import SemanticReranker
    calls = []
    def encode(self, backend, model, values):
        calls.extend(values)
        return np.array([[1., 0.] for _ in values], dtype=np.float32)
    monkeypatch.setattr(SemanticReranker, '_load_embedding_model', lambda self: ('test',
        SimpleNamespace(config=SimpleNamespace(_commit_hash='fixed-weights-v1'))))
    monkeypatch.setattr(SemanticReranker, '_encode', encode)
    def run(query, model=None):
        settings = replace(config(), embedding_model=model or config().embedding_model)
        ranker = SemanticReranker(settings)
        ranker.evidence_index = EvidenceIndex(tmp_path)
        return ranker.rank_texts(query, ['Exact passage with conditions.'])
    assert run('query one') == run('query one') == [1.]
    assert len(calls) == 2
    run('query two')
    assert len(calls) == 3  # Only the new query; passage vector is reusable.
    run('query two', 'different-embedding-model')
    assert len(calls) == 5


def test_model_pool_reuses_compatible_runtimes_and_separates_devices(monkeypatch):
    from backend.providers import runtime_pool
    from backend.stages.evidence_ranking.passages import SemanticReranker
    runtime_pool.clear()
    calls = []
    def build(self):
        calls.append(self.settings.embedding_device)
        return ('test', object())
    monkeypatch.setattr(SemanticReranker, '_create_embedding_model', build)
    one, two = SemanticReranker(config()), SemanticReranker(config())
    assert one._load_embedding_model() is two._load_embedding_model()
    other = SemanticReranker(replace(config(), embedding_device='different-device'))
    assert other._load_embedding_model() is not one._load_embedding_model()
    assert len(calls) == 2
    runtime_pool.clear()


def test_slow_source_does_not_discard_fast_results_or_cancel_parent():
    from backend.shared.cancellation import CancellationToken, cancellation_scope
    entered = Event()
    pause = Event()
    def slow():
        entered.set()
        while not current_token().requested:
            pause.wait(.005)
        check_cancelled()
    parent = CancellationToken()
    with cancellation_scope(parent):
        ready, pending = collect_searches([('slow', slow, ()), ('fast', lambda: ['actual paper'], ())], 2, .08)
        assert entered.is_set() and ready['fast'] == (['actual paper'], None)
        assert pending == ['slow'] and not parent.requested


def test_ready_papers_share_a_request_and_final_group_reuses_compatible_proof(tmp_path):
    claims, packets, answers = fixtures(2, 1)
    client = GroupClient(answers)
    observed = []
    ready = ReadyPaperJudge(client, config(), tmp_path, lambda c, d, r: observed.append(r))
    try:
        for c in claims:
            ready.add(c, packets[c['candidate_id']]['evidence_documents'][0])
        ready.finish()
        assert ready.calls == 1 and len(observed) == 2
        assert all(not r['trust']['assessment_complete'] for r in observed)
    finally:
        ready.close()
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert len(client.state['calls']) == 2  # Ready shared paper, then final Pro.
    assert stats['reused_paper_assessments'] == 2
    assert all(r['trust']['score'] == 1 and r['trust']['assessment_complete'] for r in results)


def test_expanded_final_paper_input_is_not_validated_using_an_earlier_subset(tmp_path):
    claims, packets, answers = fixtures(2, 1)
    client = GroupClient(answers)
    ready = ReadyPaperJudge(client, config(), tmp_path, lambda *args: None)
    try:
        ready.add(claims[0], packets['claim-0']['evidence_documents'][0])
        ready.finish()
    finally:
        ready.close()
    packets['claim-1']['evidence_documents'][0]['evidence_chunks'].append({
        'chunk_id': 'paper-0:extra', 'text': 'Additional restriction and opposing context.'})
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert stats['reused_paper_assessments'] == 0
    assert len(client.state['calls']) == 3  # Subset, expanded shared paper, Pro.


def test_retrieval_pending_keeps_findings_but_blocks_validated_graph(tmp_path):
    from backend.stages.graph_construction.graph import _edge_status
    from backend.stages.external_validation.aggregation import aggregate_assessment
    from backend.stages.trust_graph.ledger import evidence_balance
    claims, packets, answers = fixtures(1, 1)
    client = GroupClient(answers)
    packet = packets['claim-0']; packet['retrieval_pending'] = True
    results, _, _ = assess_group(claims, packets, client, config(), tmp_path)
    result = results[0]
    assert result['validation_status'] == 'retrieval_pending'
    assert result['trust']['score'] == 1 and not result['trust']['assessment_complete']
    assert _edge_status(claims[0], result)[0] == 'external_needs_review'


def test_pending_packet_retries_retrieval_but_reuses_completed_source_queries(tmp_path, monkeypatch):
    from backend.stages.literature_retrieval import workflow
    from backend.stages.external_validation.workflow import prepare_evidence, reusable_evidence
    claims, _, _ = fixtures(1, 0)
    candidate = claims[0]
    monkeypatch.setattr(workflow, 'evidence_query_plan', lambda *args: {'europe_pmc': ['one'], 'openalex': ['two']})
    state = {'available': False, 'pmc': 0, 'openalex': 0}
    def pmc(*args):
        state['pmc'] += 1
        return []
    def openalex(*args):
        state['openalex'] += 1
        if not state['available']:
            raise OSError('Temporarily unavailable')
        return []
    monkeypatch.setattr(workflow, 'search', pmc)
    monkeypatch.setattr(workflow, 'search_openalex', openalex)
    from backend.stages.evidence_ranking.passages import SemanticReranker
    def prepare(previous=None):
        return prepare_evidence(candidate, None, SemanticReranker(config()), config(), 8, 4,
            query_translation={}, cache=RunCache(persistent=EvidenceIndex(tmp_path)), resume_record=previous)
    packet = prepare()
    assert packet['retrieval_pending'] and len(packet['pending_sources']) == 1
    old = {'retrieval_signature': packet['retrieval_signature'], 'attempts': [packet]}
    assert not reusable_evidence(candidate, config(), old, 8, 4)
    state['available'] = True
    assert not prepare(old)['retrieval_pending']
    assert state['pmc'] == 1 and state['openalex'] == 2


def test_cancellable_metadata_deadline_aborts_http_without_leaking_headers(monkeypatch):
    import asyncio
    import httpx
    from backend.shared import metadata_http
    real_client = httpx.AsyncClient
    seen, closed = [], Event()
    async def handle(request):
        seen.append(request.url.path)
        if request.url.path == '/slow':
            try:
                await asyncio.sleep(5)
            finally:
                closed.set()
        return httpx.Response(200, json={'papers': ['exact-id']})
    monkeypatch.setattr(metadata_http.httpx, 'AsyncClient', lambda **kw:
        real_client(transport=httpx.MockTransport(handle), **kw))
    def request(path):
        return metadata_http.request_json('GET', 'https://example.test/' + path,
            headers={'Authorization': 'Bearer never-log-this'}, timeout=5, max_bytes=1000)
    completed, pending = collect_searches([('slow', request, ('slow',)), ('fast', request, ('fast',))], 2, .15)
    assert completed['fast'][0]['papers'] == ['exact-id'] and pending == ['slow']
    assert closed.is_set()


def test_embedding_weight_revision_invalidates_persistent_vectors(tmp_path, monkeypatch):
    from backend.stages.evidence_ranking.passages import SemanticReranker
    import numpy as np
    revision, calls = ['revision-one'], []
    monkeypatch.setattr(SemanticReranker, '_load_embedding_model', lambda self: ('test',
        SimpleNamespace(config=SimpleNamespace(_commit_hash=revision[0]))))
    def encode(self, backend, model, values):
        calls.extend(values)
        return np.ones((len(values), 2), dtype=np.float32)
    monkeypatch.setattr(SemanticReranker, '_encode', encode)
    for n in range(3):
        ranker = SemanticReranker(config()); ranker.evidence_index = EvidenceIndex(tmp_path)
        ranker.rank_texts('same query', ['same text'])
        if n == 1:
            revision[0] = 'revision-two'
    assert len(calls) == 4  # First revision cold/warm, then changed weights cold.
