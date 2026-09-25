"""One workspace color write at a time, and none for a pick a newer one replaced.

Every color pick reaches the server the moment it is made, and the resolver
shows it everywhere at once, so the slow ``mngr label`` write that follows must
not hold the next pick back. A burst of picks would otherwise queue one write
per pick; instead the writes for a workspace run one at a time, and a pick
still waiting for its turn when a newer one arrives is dropped unwritten.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId


class WorkspaceColorWrites(MutableModel):
    """Serializes each workspace's color writes and tells a superseded pick it has been replaced."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _write_lock_by_agent_id: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _latest_pick_by_agent_id: dict[str, int] = PrivateAttr(default_factory=dict)
    _pick_count: int = PrivateAttr(default=0)

    def register_pick(self, agent_id: AgentId) -> int:
        """Record a new pick for ``agent_id`` as its latest; return the pick's token."""
        with self._lock:
            self._pick_count += 1
            self._latest_pick_by_agent_id[str(agent_id)] = self._pick_count
            return self._pick_count

    def is_latest_pick(self, agent_id: AgentId, pick: int) -> bool:
        with self._lock:
            return self._latest_pick_by_agent_id.get(str(agent_id)) == pick

    @contextmanager
    def turn_to_write(self, agent_id: AgentId) -> Iterator[None]:
        """Hold ``agent_id``'s write slot for the duration of one label write."""
        with self._lock:
            write_lock = self._write_lock_by_agent_id.setdefault(str(agent_id), threading.Lock())
        with write_lock:
            yield
