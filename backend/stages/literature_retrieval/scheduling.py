"""Consume ready search sources with bounded deadlines and explicit pending work."""
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from backend.shared.cancellation import (
    CancellationToken, cancellation_scope, current_token, check_cancelled, submit_work,
)


def collect_searches(tasks, workers, seconds):
    """Return ready results and unfinished task IDs, never an invented empty hit.

    Each task is (identifier, function, args). Callers keep plan order for final
    ranking; readiness order is only scheduling. A child token cancels network
    requests at the deadline without stopping the parent's scientific run.
    """
    token = CancellationToken(parent=current_token())
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='search-ready')
    def invoke(function, args):
        with cancellation_scope(token):
            check_cancelled()
            return function(*args)
    pending = {submit_work(executor, invoke, function, args): identifier for identifier, function, args in tasks}
    completed = {}
    deadline = time.monotonic() + seconds
    try:
        while pending and time.monotonic() < deadline:
            check_cancelled()
            done, _ = wait(pending, timeout=min(0.1, max(0, deadline - time.monotonic())), return_when=FIRST_COMPLETED)
            for future in done:
                identifier = pending.pop(future)
                try:
                    completed[identifier] = (future.result(), None)
                except Exception as exc:
                    completed[identifier] = (None, type(exc).__name__)
        return completed, list(pending.values())
    finally:
        token.request()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
