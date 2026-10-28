"""Bounded, run-local cache. No articles or credentials are written to disk."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable
from backend.shared.cancellation import check_cancelled


class RunCache:
    def __init__(self, max_bytes: int = 32 * 1024 * 1024, *, persistent=None):
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._lock = threading.Lock()
        self._size = 0
        self.hits = 0
        self.persistent = persistent
        self._inflight: dict[str, Future] = {}

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._entries:
                self.hits += 1
                value, size = self._entries.pop(key)
                self._entries[key] = (value, size)
                return deepcopy(value)
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
            else:
                self.hits += 1
        if not owner:
            while not future.done():
                check_cancelled()
                try:
                    future.result(timeout=0.1)
                except FutureTimeout:
                    continue
            check_cancelled()
            return deepcopy(future.result())
        try:
            check_cancelled()
            value = compute()
            if value is not None and value != []:
                size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                if size <= self.max_bytes:
                    with self._lock:
                        while self._entries and self._size + size > self.max_bytes:
                            self._size -= self._entries.popitem(last=False)[1][1]
                        self._entries[key] = (deepcopy(value), size)
                        self._size += size
            future.set_result(deepcopy(value))
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "entries": len(self._entries), "bytes": self._size}
