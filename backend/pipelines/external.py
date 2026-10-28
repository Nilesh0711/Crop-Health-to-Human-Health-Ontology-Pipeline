"""Bounded lookahead: one retrieval owner overlaps the next packet with cloud judgement."""
from backend.shared.cancellation import check_cancelled, submit_work, CancellationToken, current_token, cancellation_scope
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import contextmanager


@contextmanager
def prefetch_evidence(candidates, prepare, enabled=True, *, cancel_lookahead_on_exit=False):
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evidence-prefetch') if enabled else None
    lookahead_token = CancellationToken(parent=current_token()) if cancel_lookahead_on_exit else None
    pending = []
    def prepare_with_scope(candidate):
        if lookahead_token:
            with cancellation_scope(lookahead_token):
                check_cancelled()
                return prepare(candidate)
        return prepare(candidate)
    def submit(candidate):
        if executor:
            future = submit_work(executor, prepare_with_scope, candidate)
        else:
            future = Future()
            try:
                future.set_result(prepare_with_scope(candidate))
            except Exception as exc:
                future.set_exception(exc)
        pending.append(future)
        return future
    def iterate():
        if not candidates:
            return
        current = submit(candidates[0])
        for index, candidate in enumerate(candidates):
            check_cancelled()
            following = submit(candidates[index + 1]) if executor and index + 1 < len(candidates) else None
            yield candidate, current
            pending.remove(current)
            current = following or (submit(candidates[index + 1]) if index + 1 < len(candidates) else None)
    try:
        yield iterate()
    finally:
        if lookahead_token:
            lookahead_token.request()
        for future in pending:
            future.cancel()
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
