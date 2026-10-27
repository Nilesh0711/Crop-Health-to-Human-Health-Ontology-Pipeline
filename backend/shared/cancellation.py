"""Job-scoped cooperative cancellation, propagated into bounded worker pools."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import Event


class PipelineStopped(BaseException):
    """Control flow: stopping must never become a scientific rejection or retry."""


class CancellationToken:
    def __init__(self):
        self._event = Event()

    def request(self):
        self._event.set()

    @property
    def requested(self):
        return self._event.is_set()

    def check(self):
        if self.requested:
            raise PipelineStopped('Stopped by user. Saved checkpoints are preserved.')


_current = ContextVar('pipeline_cancellation', default=None)


def current_token():
    return _current.get()


def check_cancelled():
    token = current_token()
    if token:
        token.check()


@contextmanager
def cancellation_scope(token):
    binding = _current.set(token)
    try:
        yield
    finally:
        _current.reset(binding)


def submit_work(executor, function, *args):
    context = copy_context()
    def invoke():
        check_cancelled()
        return function(*args)
    return executor.submit(context.run, invoke)
