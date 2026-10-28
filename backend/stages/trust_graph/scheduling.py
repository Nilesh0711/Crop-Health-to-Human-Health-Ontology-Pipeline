"""Bounded independent requests; checkpoint successes even if a sibling fails."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from backend.shared.cancellation import check_cancelled, submit_work


def parallel_papers(items, function, workers):
    """Yield completed work to a single checkpoint owner, with no hidden retry.

    At most `workers` requests are submitted. After a failure no further work
    starts; already submitted successes are yielded before the error is raised.
    Cancellation remains control flow and propagates into provider requests.
    """
    if workers == 1:
        for item in items:
            check_cancelled()
            yield item, function(item)
        return
    iterator = iter(enumerate(items))
    pending = {}
    failure = None
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='trust-paper')

    def submit_next():
        try:
            order, item = next(iterator)
        except StopIteration:
            return
        pending[submit_work(executor, function, item)] = (order, item)

    try:
        for _ in range(workers):
            submit_next()
        while pending:
            check_cancelled()
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            completed = []
            for future in sorted(done, key=lambda f: pending[f][0]):
                _, item = pending.pop(future)
                try:
                    completed.append((item, future.result()))
                except Exception as exc:
                    failure = failure or exc
            for result in completed:
                yield result
            if failure is None:
                for _ in done:
                    submit_next()
        if failure is not None:
            raise failure
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
