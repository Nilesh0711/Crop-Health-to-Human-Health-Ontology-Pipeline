"""Reuse compatible local model runtimes; serialize access per device."""
from contextlib import contextmanager
from threading import RLock

_lock = RLock()
_runtimes = {}
_devices = {}


def runtime(key, factory):
    with _lock:
        if key not in _runtimes:
            _runtimes[key] = factory()
        return _runtimes[key]


@contextmanager
def inference(device):
    with _lock:
        guard = _devices.setdefault(device, RLock())
    with guard:
        yield


def clear():
    """Explicit process maintenance/testing hook; never called during a run."""
    with _lock:
        _runtimes.clear()
