"""Bounded lookahead: one retrieval owner overlaps the next packet with cloud judgement."""
from backend.shared.cancellation import check_cancelled, submit_work
from concurrent.futures import ThreadPoolExecutor, Future
from contextlib import contextmanager


@contextmanager
def prefetch_evidence(candidates, prepare, enabled=True):
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evidence-prefetch') if enabled else None
    pending = []
    def submit(candidate):
        if executor:
            future = submit_work(executor, prepare, candidate)
        else:
            future = Future()
            try:
                future.set_result(prepare(candidate))
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
        for future in pending:
            future.cancel()
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
