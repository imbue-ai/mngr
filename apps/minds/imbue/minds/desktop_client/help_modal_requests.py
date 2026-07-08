"""Broker that lets a backend request the chrome open the get-help modal.

An in-workspace agent escalates a bug report by POSTing its diagnosis to the
``/api/v1/agents/<id>/report`` route. Rather than submit to Sentry itself, that
route asks the running desktop app to open the report-a-bug modal pre-filled
with the agent's description, so a human reviews and submits it. This broker is
the fan-out point between the route handler (the producer, on a Flask request
thread) and the chrome-events SSE generators (the consumers, one per connected
window): each SSE generator subscribes its per-connection queue + wake event,
and ``request_open`` pushes the request onto every subscriber's queue and wakes
its loop, so every window gets the chance to surface the modal.

The request is fire-and-forget: if no window is subscribed, it is dropped (there
is nowhere to show the modal). The broker pushes directly onto subscriber queues
rather than invoking callbacks, mirroring the per-connection queue + ``Event``
wake the SSE loop already uses for health transitions.
"""

import queue
import threading

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


class HelpModalRequestBroker(MutableModel):
    """Fans ``OpenHelpRequest``s out to every subscribed chrome-events SSE connection.

    Each subscriber is a ``(queue, wake_event)`` pair owned by one SSE
    connection: ``request_open`` enqueues the request and sets the event so the
    connection's loop drains it. Subscriptions are added on connect and removed
    in the loop's ``finally``.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _subscribers: list[tuple["queue.Queue[OpenHelpRequest]", threading.Event]] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def subscribe(self, request_queue: "queue.Queue[OpenHelpRequest]", wake_event: threading.Event) -> None:
        """Register an SSE connection's queue + wake event to receive open-help requests."""
        with self._lock:
            self._subscribers.append((request_queue, wake_event))

    def unsubscribe(self, request_queue: "queue.Queue[OpenHelpRequest]", wake_event: threading.Event) -> None:
        """Unregister a previously subscribed queue + wake event. No-op if not subscribed."""
        with self._lock:
            try:
                self._subscribers.remove((request_queue, wake_event))
            except ValueError:
                pass

    def request_open(self, request: OpenHelpRequest) -> None:
        """Enqueue ``request`` onto every subscribed connection's queue and wake each one."""
        with self._lock:
            subscribers = list(self._subscribers)
        for request_queue, wake_event in subscribers:
            request_queue.put(request)
            wake_event.set()
