"""Early fixed-input paper assessments; final expanded inputs are rechecked."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import time

from backend.shared.cancellation import (
    CancellationToken, current_token, cancellation_scope, check_cancelled, submit_work,
)
from backend.stages.trust_graph.workflow import assess_group
from backend.stages.trust_graph.judgement import paper_signature


class ReadyPaperJudge:
    def __init__(self, client, settings, cache_dir, on_result, previous=None):
        self.client, self.settings, self.cache_dir = client, settings, cache_dir
        self.on_result, self.previous = on_result, previous or {}
        self.queue, self.running, self.seen = deque(), {}, set()
        self.executor = ThreadPoolExecutor(max_workers=settings.external_paper_workers, thread_name_prefix='paper-ready')
        self.token = CancellationToken(parent=current_token())
        self.failure = None
        self.calls, self.reused = 0, 0

    def add(self, candidate, document):
        signature = paper_signature(candidate, document, self.settings.external_paper_model, self.settings.task_thinking['external'])
        key = (candidate['candidate_id'], signature)
        if key not in self.seen:
            self.seen.add(key)
            self.queue.append((deepcopy(candidate), deepcopy(document)))

    def _request(self, members):
        with cancellation_scope(self.token):
            check_cancelled()
            prepared = {}
            for candidate, document in members:
                cid = candidate['candidate_id']
                old = self.previous.get(cid, {}).get('attempts') or [{}]
                prepared[cid] = {'evidence_documents': [document], 'retrieval_pending': True,
                                'paper_judgements': old[-1].get('paper_judgements', [])}
            result, _, stats = assess_group([c for c, _ in members], prepared, self.client,
                replace(self.settings, external_paper_workers=1), self.cache_dir, paper_only=True)
            return result, stats

    def poll(self):
        check_cancelled()
        for future in list(self.running):
            if not future.done():
                continue
            members = self.running.pop(future)
            try:
                results, stats = future.result()
                self.calls += stats['primary_calls']
                self.reused += stats['reused_paper_assessments']
                for result in results:
                    candidate = next(c for c, _ in members if c['candidate_id'] == result['candidate_id'])
                    self.on_result(candidate, result['attempts'][-1]['evidence_documents'][0], result)
            except Exception as exc:
                self.failure = self.failure or exc
        if self.failure is None:
            while self.queue and len(self.running) < self.settings.external_paper_workers:
                first = self.queue.popleft()
                members = [first]
                for item in list(self.queue):
                    if len(members) < self.settings.trust_batch_size and item[1]['evidence_document_id'] == first[1]['evidence_document_id']:
                        self.queue.remove(item)
                        members.append(item)
                self.running[submit_work(self.executor, self._request, members)] = members
        elif not self.running:
            raise self.failure

    def finish(self):
        while self.running or self.queue:
            self.poll()
            if self.failure and not self.running:
                raise self.failure
            time.sleep(0.02)

    def close(self):
        self.token.request()
        for future in self.running:
            future.cancel()
        self.executor.shutdown(wait=True, cancel_futures=True)
