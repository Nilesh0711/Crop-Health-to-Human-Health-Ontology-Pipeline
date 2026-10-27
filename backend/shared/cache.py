"""Bounded, run-local cache. No articles or credentials are written to disk."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from collections import OrderedDict
from typing import Any, Callable


class RunCache:
    def __init__(self, max_bytes: int = 32 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._lock = threading.Lock()
        self._size = 0
        self.hits = 0

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._entries:
                self.hits += 1
                value, size = self._entries.pop(key)
                self._entries[key] = (value, size)
                return deepcopy(value)
        value = compute()
        if value is None or value == []:
            return value
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size <= self.max_bytes:
            with self._lock:
                if key in self._entries:
                    self._size -= self._entries.pop(key)[1]
                while self._entries and self._size + size > self.max_bytes:
                    self._size -= self._entries.popitem(last=False)[1][1]
                self._entries[key] = (deepcopy(value), size)
                self._size += size
        return value

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "entries": len(self._entries), "bytes": self._size}
