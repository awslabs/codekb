"""Per-container cache of a user's accessible repos. LRU + a config'd max-age.

Key is ``(sub, provider, host)`` so cloud and self-hosted don't mix.
Saving/deleting a token clears the user's entries here; other containers catch
up within the max-age.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from ..core.config import get_positive_int

_MS_PER_HOUR = 3_600_000

CacheKey = tuple[str, str, str]

_MISS = object()


def build_key(sub: str, provider: str, host: str) -> CacheKey:
    """The one key builder used by both reads and invalidation."""
    return (sub, provider, (host or "").lower())


class RepoAccessCache:
    """Bounded, TTL'd map from a key to a value.

    Stores a repo listing, or an empty map for the "no usable token" case so a
    tokenless caller isn't re-read per space. Expired/unavailable are never cached.
    """

    def __init__(self, max_age_ms: Optional[int] = None, maxsize: Optional[int] = None) -> None:
        if maxsize is not None and maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self._store: "OrderedDict[CacheKey, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_age_ms = max_age_ms
        self._maxsize = maxsize
        # Config is static after startup — resolve ttl/capacity once, not per access.
        self._ttl_ms_memo: Optional[int] = None
        self._capacity_memo: Optional[int] = None

    def _ttl_ms(self) -> int:
        if self._max_age_ms is not None:
            return self._max_age_ms
        if self._ttl_ms_memo is None:
            self._ttl_ms_memo = get_positive_int("authz.repo_cache_ttl_hours") * _MS_PER_HOUR
        return self._ttl_ms_memo

    def _capacity(self) -> int:
        if self._maxsize is not None:  # validated >= 1 in __init__
            return self._maxsize
        if self._capacity_memo is None:
            self._capacity_memo = get_positive_int("authz.repo_cache_maxsize")
        return self._capacity_memo

    def get(self, key: CacheKey) -> Any:
        """Return the value (refreshing LRU order), or ``_MISS`` if absent/expired."""
        now = time.monotonic() * 1000.0
        with self._lock:
            entry = self._store.get(key, _MISS)
            if entry is _MISS:
                return _MISS
            stored_at, value = entry  # type: ignore[misc]
            if now - stored_at > self._ttl_ms():
                self._store.pop(key, None)
                return _MISS
            self._store.move_to_end(key)
            return value

    def put(self, key: CacheKey, value: Any) -> None:
        now = time.monotonic() * 1000.0
        with self._lock:
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self._capacity():
                self._store.popitem(last=False)

    def invalidate_user(self, sub: str, provider: str) -> int:
        """Drop every entry for ``(sub, provider)``. Returns the count removed."""
        with self._lock:
            victims = [k for k in self._store if k[0] == sub and k[1] == provider]
            for k in victims:
                self._store.pop(k, None)
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


_cache = RepoAccessCache()


def get_cache() -> RepoAccessCache:
    return _cache


def invalidate_user(sub: str, provider: str) -> int:
    """Clear a user's cached permissions for a provider (after a token save/delete)."""
    return _cache.invalidate_user(sub, provider)


MISS = _MISS
