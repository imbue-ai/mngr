"""A thread-safe, short-TTL cache of per-host lookups, keyed by host id.

The sharing readiness poll fires every ~2 seconds and each of its reads is a
multi-second subprocess (a connector share lookup, an exec into the
workspace), so back-to-back polls reuse one read. The sharing PUT/DELETE
handlers invalidate their host's entry so state flips are observed
immediately rather than at TTL expiry.
"""

import threading
import time
from typing import Generic
from typing import TypeVar

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

CachedValueT = TypeVar("CachedValueT")


class CachedLookup(FrozenModel, Generic[CachedValueT]):
    """One cached lookup; ``value`` may legitimately be None (a cached negative answer is a hit, not a miss)."""

    value: CachedValueT = Field(description="The looked-up value as the read returned it")


class HostKeyedTtlCache(MutableModel, Generic[CachedValueT]):
    """Cache of one lookup result per host id, each reusable for ``ttl_seconds``."""

    ttl_seconds: float = Field(frozen=True, description="How long one lookup may be reused")
    _lookup_and_deadline_by_host_id: dict[str, tuple[float, CachedLookup[CachedValueT]]] = PrivateAttr(
        default_factory=dict
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def get(self, host_id: str) -> CachedLookup[CachedValueT] | None:
        """The unexpired cached lookup for ``host_id``, or None on a miss."""
        with self._lock:
            entry = self._lookup_and_deadline_by_host_id.get(host_id)
            if entry is None:
                return None
            deadline, lookup = entry
            if time.monotonic() >= deadline:
                del self._lookup_and_deadline_by_host_id[host_id]
                return None
            return lookup

    def put(self, host_id: str, value: CachedValueT) -> None:
        with self._lock:
            self._lookup_and_deadline_by_host_id[host_id] = (
                time.monotonic() + self.ttl_seconds,
                CachedLookup[CachedValueT](value=value),
            )

    def invalidate(self, host_id: str) -> None:
        with self._lock:
            self._lookup_and_deadline_by_host_id.pop(host_id, None)
