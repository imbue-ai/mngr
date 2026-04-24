"""Level-triggered health state for each agent's workspace_server.

The state is authoritative: consumers (SSE subscribers, the /api/agents/{id}/health
probe endpoint, main-page polling) can ask "what is this agent's current status?"
and get a definitive answer, so a subscriber connecting mid-incident picks up the
stuck state without needing to have witnessed the failing transition.

Transitions:

- Any connection-level proxy failure (Timeout/Connect/ReadError/RemoteProtocol)
  flips the agent to ``STUCK``. A single failure is enough -- we don't wait for
  a threshold because real users don't retry enough times to hit one.
- Any 2xx/3xx proxy success flips the agent to ``HEALTHY``. 4xx/5xx do NOT
  recover a stuck server: a 500 from the upstream workspace_server is itself a
  sign it is unhealthy.
- A restart click flips the agent to ``RESTARTING``; the next proxy observation
  (success or failure) moves it out of that state.

Absent from the map = never observed. Callers that care about "unknown" (e.g.
the landing-page polling that seeds the state) explicitly probe and record.
"""

import threading
from collections.abc import Callable
from enum import auto

from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel


class AgentHealth(UpperCaseStrEnum):
    HEALTHY = auto()
    STUCK = auto()
    RESTARTING = auto()


class WorkspaceServerHealthTracker(MutableModel):
    """Level-triggered health state for each agent's workspace_server.

    Thread-safe: the proxy layer calls ``record_failure`` / ``record_success``
    from whichever AnyIO worker handles a request; the chrome events SSE
    generator reads ``snapshot_all`` and subscribes via
    ``add_on_change_callback``.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _states: dict[str, AgentHealth] = PrivateAttr(default_factory=dict)
    _callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)

    def record_failure(self, agent_id: str) -> None:
        """Mark the agent as stuck."""
        self._set_state(agent_id, AgentHealth.STUCK)

    def record_success(self, agent_id: str) -> None:
        """Mark the agent as healthy."""
        self._set_state(agent_id, AgentHealth.HEALTHY)

    def mark_restarting(self, agent_id: str) -> None:
        """Mark the agent as restarting. Cleared by the next proxy observation."""
        self._set_state(agent_id, AgentHealth.RESTARTING)

    def get_health(self, agent_id: str) -> AgentHealth | None:
        """Return the current health level for this agent, or None if never observed."""
        with self._lock:
            return self._states.get(agent_id)

    def snapshot_all(self) -> dict[str, AgentHealth]:
        """Return a copy of the current health map."""
        with self._lock:
            return dict(self._states)

    def add_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when any agent's state transitions.

        Callbacks are invoked OUTSIDE the lock so they can safely call back
        into this class or do IO.
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Unregister a callback. Silent no-op if the callback wasn't registered."""
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def _set_state(self, agent_id: str, new_state: AgentHealth) -> None:
        with self._lock:
            previous = self._states.get(agent_id)
            if previous == new_state:
                return
            self._states[agent_id] = new_state
            callbacks_to_run = list(self._callbacks)

        for callback in callbacks_to_run:
            callback()
