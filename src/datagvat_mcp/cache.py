"""Small in-memory TTL + LRU cache."""

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

MISSING: Any = object()


class TTLCache:
    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> Any:
        item = self._data.get(key)
        if item is None:
            self.misses += 1
            return MISSING
        expires, value = item
        if expires <= self._clock():
            del self._data[key]
            self.misses += 1
            return MISSING
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: Hashable, value: Any, ttl: float) -> None:
        if ttl <= 0 or self.max_entries <= 0:
            return
        self._data[key] = (self._clock() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
