"""Bounded grounding requests; only the owner publishes completed checkpoints."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from backend.shared.cancellation import (CancellationToken, cancellation_scope,
    current_token, check_cancelled, submit_work)


def run_packets(packets, assess, publish, workers):
    token = CancellationToken(current_token())
    pending, exhausted = {}, False
    packets = iter(packets)
    with cancellation_scope(token):
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ontology-grounding')
        try:
            while pending or not exhausted:
                check_cancelled()
                while not exhausted and len(pending) < workers:
                    packet = next(packets, None)
                    exhausted = packet is None
                    if not exhausted:
                        pending[submit_work(executor, assess, packet)] = packet['number']
                if not pending:
                    break
                ready, _ = wait(pending, return_when=FIRST_COMPLETED)
                failure = None
                # Publish every completed success even if another ready batch failed.
                for future in sorted(ready, key=pending.get):
                    pending.pop(future)
                    try:
                        rows = future.result()
                    except BaseException as exc:
                        failure = failure or exc
                    else:
                        publish(rows)
                if failure is not None:
                    raise failure
        except BaseException:
            token.request('Grounding interrupted; completed checkpoints are preserved')
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            # A sibling may have finished just before cancellation. Keep its result.
            for future in pending:
                if not future.cancelled():
                    try:
                        rows = future.result()
                    except BaseException:
                        continue
                    publish(rows)
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
