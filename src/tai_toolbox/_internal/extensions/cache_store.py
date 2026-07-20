"""Value store and key serialization for the ``cache`` tool extension.

Each ``cache`` branch owns one :class:`CacheStore` — a per-key value store with
per-key single-flight locks — so branches never share cached results. The store
is separate from the makefun factory that presents the cached tool's signature.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai_kit.settings import TaiBaseSettings, settings_cache

# A miss sentinel distinct from any real cached value, so ``None`` can be cached
# and read back without being mistaken for a miss.
MISS = object()


class CacheSettings(TaiBaseSettings):
    """Bound on the number of live entries a single cache branch retains."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    max_entries: int = Field(default=1024)


@settings_cache
def cache_settings() -> CacheSettings:
    return CacheSettings()


def compute_key(*args: Any, **kwargs: Any) -> str:
    """Serialize call arguments into a compact, stable cache key."""
    # JSON with sorted keys and no whitespace gives a compact, stable key.
    key_data = [args, kwargs]
    return json.dumps(key_data, sort_keys=True, default=str, separators=(",", ":"))


class CacheStore:
    """One cache branch's value store with per-key single-flight locks.

    One lock per key so identical concurrent calls single-flight instead of all
    missing and re-running the wrapped tool (a cache stampede that defeats caching
    whenever the calls overlap). A key's lock is dropped once its value is cached,
    so :attr:`_key_locks` only ever holds locks for keys currently being computed.

    The store holds at most ``max_entries`` live entries. A ``write`` that would
    exceed the cap evicts least-recently-used entries, after first reclaiming any
    whose TTL has elapsed. A key that still holds a single-flight lock is never
    evicted, so eviction cannot drop a value a concurrent caller is about to read.

    Expiry is stamped and checked against :func:`time.monotonic`, a process-local
    duration clock, so a wall-clock change never extends live TTLs or serves stale
    entries early.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        cap = cache_settings().max_entries if max_entries is None else max_entries
        if cap <= 0:
            raise ValueError(f"CacheStore max_entries must be positive, got {cap}")
        self._max_entries = cap
        self._values: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._key_locks: dict[str, asyncio.Lock] = {}

    def read(self, key: str) -> Any:
        """Return the live cached value for ``key``, or :data:`MISS` when absent
        or expired (evicting an expired entry). A hit marks ``key`` most recently
        used for LRU ordering."""
        try:
            value, expire = self._values[key]
        except KeyError:
            return MISS
        if expire is None or time.monotonic() < expire:
            self._values.move_to_end(key)
            return value
        del self._values[key]
        return MISS

    def key_lock(self, key: str) -> asyncio.Lock:
        """The single-flight lock for ``key`` (created on first use)."""
        return self._key_locks.setdefault(key, asyncio.Lock())

    def write(self, key: str, value: Any, exp: float | None) -> None:
        """Store ``value`` under ``key`` with a ``exp``-seconds TTL (``None`` or a
        non-positive ``exp`` stores it without expiry), enforcing the entry cap."""
        expire = time.monotonic() + exp if (exp is not None and exp > 0) else None
        # Reclaim TTL'd slots before enforcing the cap, so expired entries are
        # dropped in preference to evicting live ones.
        self._drop_expired()
        self._values[key] = (value, expire)
        self._values.move_to_end(key)
        self._evict_to_cap()

    def drop_lock(self, key: str) -> None:
        """Drop ``key``'s single-flight lock once its value is cached."""
        self._key_locks.pop(key, None)

    def _drop_expired(self) -> None:
        """Remove every entry whose TTL has elapsed."""
        now = time.monotonic()
        expired = [key for key, (_, expire) in self._values.items() if expire is not None and expire <= now]
        for key in expired:
            del self._values[key]

    def _evict_to_cap(self) -> None:
        """Evict least-recently-used entries until the store is within its cap.

        Keys still holding a single-flight lock are skipped: their value is being
        computed or is about to be read by a waiting caller, so evicting them would
        force a redundant recompute. When every over-cap entry is locked the store
        stays temporarily above the cap rather than dropping an in-use value.
        """
        while len(self._values) > self._max_entries:
            victim = next((key for key in self._values if key not in self._key_locks), None)
            if victim is None:
                break
            del self._values[victim]
