from __future__ import annotations

import structlog
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

log = structlog.get_logger()

T = TypeVar("T")


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    size: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class BaseCache(ABC, Generic[T]):
    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 100, label: str = ""):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._label = label
        self._stats = CacheStats()

    @abstractmethod
    def _get(self, key: str) -> T | None:
        ...

    @abstractmethod
    def _put(self, key: str, value: T):
        ...

    @abstractmethod
    def _remove(self, key: str) -> bool:
        ...

    @abstractmethod
    def _clear(self):
        ...

    @abstractmethod
    def _size(self) -> int:
        ...

    def get(self, key: str) -> T | None:
        result = self._get(key)
        if result is not None:
            self._stats.hits += 1
        else:
            self._stats.misses += 1
        return result

    def put(self, key: str, value: T):
        self._put(key, value)

    def invalidate(self, key: str) -> bool:
        return self._remove(key)

    def clear(self):
        self._clear()
        self._stats = CacheStats()

    @property
    def stats(self) -> CacheStats:
        self._stats.size = self._size()
        return self._stats

    @property
    def label(self) -> str:
        return self._label


class TTLCache(BaseCache[T]):
    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 100, label: str = ""):
        super().__init__(ttl_seconds=ttl_seconds, max_entries=max_entries, label=label)
        self._store: dict[str, tuple[float, T]] = {}

    def _get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            self._stats.evictions += 1
            return None
        return value

    def _put(self, key: str, value: T):
        if len(self._store) >= self._max and key not in self._store:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]
            self._stats.evictions += 1
        self._store[key] = (time.time(), value)

    def _remove(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    def _clear(self):
        self._store.clear()

    def _size(self) -> int:
        return len(self._store)

    def invalidate_prefix(self, prefix: str):
        keys_to_del = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_del:
            del self._store[k]
            self._stats.evictions += 1


class NamespacedCache:
    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 100):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._caches: dict[str, TTLCache] = {}

    def namespace(self, name: str) -> TTLCache:
        if name not in self._caches:
            self._caches[name] = TTLCache(
                ttl_seconds=self._ttl, max_entries=self._max, label=name,
            )
        return self._caches[name]

    def clear_all(self):
        for c in self._caches.values():
            c.clear()

    def all_stats(self) -> dict[str, CacheStats]:
        return {name: c.stats for name, c in self._caches.items()}
