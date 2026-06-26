"""Broker that lets a backend request the chrome open the get-help modal.

An in-workspace agent escalates a bug report by POSTing its diagnosis to the
``/api/v1/agents/<id>/report`` route. Rather than submit to Sentry itself, that
route asks the running desktop app to open the report-a-bug modal pre-filled
with the agent's description, so a human reviews and submits it. This broker is
the fan-out point between the route handler (the producer, on a Flask request
thread) and the chrome-events SSE generators (the consumers, one per connected
window): each SSE generator registers a callback, and ``request_open`` invokes
all of them so every window gets the chance to surface the modal.

The request is fire-and-forget: if no window is currently subscribed, it is
dropped (there is nowhere to show the modal). Callbacks must be fast and
non-blocking -- each SSE generator's callback only enqueues onto its own
per-connection queue and wakes its loop -- because they run on the producer's
thread, mirroring :class:`SystemInterfaceHealthTracker`'s on-change callbacks.
"""

import threading
from collections.abc import Callable

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class OpenHelpRequest(FrozenModel):
    """A request to open the get-help modal pre-filled for a human to submit."""

    description: str = Field(description="Pre-filled report description (the agent's diagnosis)")
    workspace_agent_id: str | None = Field(
        default=None,
        description="The workspace the report is scoped to, so the chrome opens the modal in that window.",
    )


OpenHelpCallback = Callable[[OpenHelpRequest], None]


class HelpModalRequestBroker(MutableModel):
    """Fans ``OpenHelpRequest``s out to every subscribed chrome-events SSE connection."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _callbacks: list[OpenHelpCallback] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def add_on_request_callback(self, callback: OpenHelpCallback) -> None:
        """Register a callback fired for every ``request_open`` call.

        Callbacks run on the producer's thread and must be fast and
        non-blocking (enqueue + wake only).
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_on_request_callback(self, callback: OpenHelpCallback) -> None:
        """Unregister a previously registered callback. No-op if not registered."""
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def request_open(self, request: OpenHelpRequest) -> None:
        """Notify every subscribed connection to open the help modal for ``request``."""
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            callback(request)
