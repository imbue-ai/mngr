"""Broker that fans one-shot chrome-events SSE payloads out to every connection.

The ``/_chrome/events`` stream mostly re-derives its payloads from resolver
state each tick, but some events are edge-triggered facts that must reach every
connected window exactly when they happen:

- ``workspace_stopped``: a workspace's host was stopped through an in-app
  action, so any window open to it should close rather than observe the dead
  interface and auto-restart it.
- ``open_help``: an in-workspace agent escalated a bug report, so every window
  gets the chance to surface the report modal pre-filled with its diagnosis.

This broker is the fan-out point between the producer (a Flask request thread)
and the chrome-events SSE generators (one per connected window): each generator
subscribes its per-connection queue + wake event, and ``broadcast`` pushes the
payload onto every subscriber's queue and wakes its loop. Each payload is the
finished SSE frame (a ``{"type": ...}`` dict), built by the helpers below.

Events are fire-and-forget: with no subscriber the payload is dropped (there is
no window to act on it). The broker pushes directly onto subscriber queues
rather than invoking callbacks, mirroring the per-connection queue + ``Event``
wake the SSE loop already uses for health transitions.
"""

import queue
import threading
from collections.abc import Mapping

from pydantic import ConfigDict
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel


class ChromeEventBroadcaster(MutableModel):
    """Fans one-shot SSE payload dicts out to every subscribed chrome-events connection.

    Each subscriber is a ``(queue, wake_event)`` pair owned by one SSE
    connection: ``broadcast`` enqueues the payload and sets the event so the
    connection's loop drains it. Subscriptions are added on connect and removed
    in the loop's ``finally``.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _subscribers: list[tuple["queue.Queue[dict[str, str]]", threading.Event]] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def subscribe(self, event_queue: "queue.Queue[dict[str, str]]", wake_event: threading.Event) -> None:
        """Register an SSE connection's queue + wake event to receive broadcast payloads."""
        with self._lock:
            self._subscribers.append((event_queue, wake_event))

    def unsubscribe(self, event_queue: "queue.Queue[dict[str, str]]", wake_event: threading.Event) -> None:
        """Unregister a previously subscribed queue + wake event. No-op if not subscribed."""
        with self._lock:
            try:
                self._subscribers.remove((event_queue, wake_event))
            except ValueError:
                pass

    def broadcast(self, payload: Mapping[str, str]) -> None:
        """Enqueue ``payload`` onto every subscribed connection's queue and wake each one."""
        with self._lock:
            subscribers = list(self._subscribers)
        for event_queue, wake_event in subscribers:
            event_queue.put(dict(payload))
            wake_event.set()


def build_workspace_stopped_payload(workspace_agent_id: str) -> dict[str, str]:
    """The ``workspace_stopped`` SSE payload: an in-app action stopped this workspace's host.

    Consumed by the Electron shell (close any window open to the workspace, so
    an open view can't observe the dead interface and auto-restart it, undoing
    the stop) and by the browser-mode chrome (navigate the content frame home).
    """
    return {"type": "workspace_stopped", "agent_id": workspace_agent_id}


def build_open_help_payload(description: str, workspace_agent_id: str) -> dict[str, str]:
    """The ``open_help`` SSE payload: open the report-a-bug modal pre-filled for a human to submit.

    Emitted when an in-workspace agent POSTs a bug report to
    ``/api/v1/agents/<id>/report`` rather than submitting to Sentry itself.
    ``description`` is the agent's diagnosis and ``workspace_agent_id`` scopes
    the modal to the reporting workspace's window.
    """
    return {"type": "open_help", "description": description, "workspace_agent_id": workspace_agent_id}
