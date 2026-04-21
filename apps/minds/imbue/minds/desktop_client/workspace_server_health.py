"""Tracks consecutive proxy failures per (agent_id, server_name) and surfaces
'stuck server' signals so the chrome UI can show a toast with a restart button.

The failure-window bookkeeping is intentionally coarse: the goal is not to
distinguish every error class but to answer "is this backend wedged right now
or just transiently slow?" When it crosses the threshold we fire callbacks so
the chrome SSE stream can push a `workspace_server_status` event.
"""

import threading
import time
from collections.abc import Callable
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

_DEFAULT_WINDOW_SECONDS: Final[float] = 30.0
_DEFAULT_FAILURE_THRESHOLD: Final[int] = 3


class StuckServerInfo(MutableModel):
    """Snapshot of a (agent_id, server_name) pair that the tracker considers stuck."""

    agent_id: str = Field(description="Agent ID whose backend is misbehaving")
    server_name: str = Field(description="Server key (e.g. system_interface, web) inside that agent")
    stuck_since: float = Field(description="UNIX timestamp of the oldest failure in the current streak")
    failure_count: int = Field(description="Number of failures currently within the rolling window")
    last_error_class: str = Field(description="Error class name of the most recent failure (e.g. TimeoutException)")
    last_error_time: float = Field(description="UNIX timestamp of the most recent failure")


class WorkspaceServerHealthTracker(MutableModel):
    """Counts proxy failures per (agent_id, server_name) and exposes stuck signals.

    Thread-safe: the proxy layer calls ``record_failure`` / ``record_success`` from
    whichever AnyIO worker handles a request, while the chrome events SSE
    generator reads ``snapshot_stuck`` and subscribes via ``add_on_change_callback``.
    """

    window_seconds: float = Field(
        default=_DEFAULT_WINDOW_SECONDS,
        description="Trailing-window size over which failures are counted",
    )
    failure_threshold: int = Field(
        default=_DEFAULT_FAILURE_THRESHOLD,
        description="Number of failures within the window that marks a server stuck",
    )
    clock: Callable[[], float] = Field(
        default=time.time,
        description=(
            "Callable returning the current UNIX timestamp. Override via constructor "
            "to inject a deterministic clock in tests."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _failures: dict[tuple[str, str], list[float]] = PrivateAttr(default_factory=dict)
    _stuck: dict[tuple[str, str], StuckServerInfo] = PrivateAttr(default_factory=dict)
    _callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)

    def record_failure(self, agent_id: str, server_name: str, error_class: str) -> None:
        """Note a proxy failure for (agent_id, server_name).

        Fires on-change callbacks when a server transitions from healthy to stuck.
        """
        now = self.clock()
        fired = False
        with self._lock:
            key = (agent_id, server_name)
            timestamps = self._failures.setdefault(key, [])
            cutoff = now - self.window_seconds
            # Purge entries older than the rolling window.
            kept: list[float] = [ts for ts in timestamps if ts >= cutoff]
            kept.append(now)
            self._failures[key] = kept

            if len(kept) >= self.failure_threshold:
                already_stuck = key in self._stuck
                self._stuck[key] = StuckServerInfo(
                    agent_id=agent_id,
                    server_name=server_name,
                    stuck_since=kept[0],
                    failure_count=len(kept),
                    last_error_class=error_class,
                    last_error_time=now,
                )
                fired = not already_stuck
            callbacks_to_run = list(self._callbacks) if fired else []

        for callback in callbacks_to_run:
            callback()

    def record_success(self, agent_id: str, server_name: str) -> None:
        """Note a proxy success for (agent_id, server_name). Clears the stuck marker if any."""
        fired = False
        with self._lock:
            key = (agent_id, server_name)
            self._failures.pop(key, None)
            if key in self._stuck:
                del self._stuck[key]
                fired = True
            callbacks_to_run = list(self._callbacks) if fired else []

        for callback in callbacks_to_run:
            callback()

    def snapshot_stuck(self) -> tuple[StuckServerInfo, ...]:
        """Return an immutable snapshot of currently-stuck servers."""
        with self._lock:
            return tuple(self._stuck.values())

    def add_on_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback fired when the stuck-set changes.

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
