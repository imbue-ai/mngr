"""Background streaming consumer for the ``permission-requests`` extension.

Spawned at desktop-client startup, owns a daemon thread that holds a
long-lived ``GET /permission-requests?follow=true`` connection open
against the shared latchkey gateway. Each pending request streamed
over that connection is translated into a
:class:`LatchkeyPredefinedPermissionRequestEvent` and appended to the in-memory
:class:`RequestInbox` exactly the way agent-written JSONL events used
to be (before latchkey 2.9.0 grew the extension layer).

The consumer must be resilient: a network blip or a gateway restart
will tear the stream down, but the on-disk request files survive, so
the consumer just reconnects with bounded backoff and the next stream
re-emits everything that is still pending. There is no need to
deduplicate -- the inbox keys requests by ``event_id``, which we
populate from the extension-generated ``request_id``, so a redelivered
request idempotently overwrites the previous entry.
"""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
from imbue.minds.desktop_client.request_events import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestType

# Backoff bounds for the reconnect loop. The lower bound keeps the
# consumer responsive when the gateway is just slow to start; the upper
# bound prevents pathological busy-looping if the gateway dies and is
# never restarted.
_RECONNECT_MIN_DELAY_SECONDS: Final[float] = 1.0
_RECONNECT_MAX_DELAY_SECONDS: Final[float] = 30.0
_RECONNECT_DELAY_GROWTH: Final[float] = 2.0


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def streamed_request_to_event(streamed: StreamedPermissionRequest) -> RequestEvent:
    """Translate a streamed permission request into the inbox's event shape.

    ``request_id`` from the extension is reused verbatim as the inbox
    ``event_id`` so the FastAPI routes (which look events up by
    ``event_id`` and DELETE the gateway record on grant/deny) can join
    the two systems on a single identifier.

    Dispatches on the concrete type of ``streamed.payload``:
    :class:`PredefinedRequestPayload` becomes a
    :class:`LatchkeyPredefinedPermissionRequestEvent` (the legacy scope/perm
    grant flow); :class:`FileSharingRequestPayload` becomes a
    :class:`LatchkeyFileSharingPermissionRequestEvent` (rendered as a single
    per-path yes/no dialog whose grant path goes through
    ``POST /permission-requests/approve/<id>``).
    """
    payload = streamed.payload
    if isinstance(payload, PredefinedRequestPayload):
        return LatchkeyPredefinedPermissionRequestEvent(
            timestamp=_now_iso(),
            type=EventType("latchkey_permission_request"),
            event_id=EventId(streamed.request_id),
            source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
            agent_id=streamed.agent_id,
            request_type=str(RequestType.LATCHKEY_PERMISSION),
            is_user_requested=False,
            permissions_target_path=streamed.target,
            scope=payload.scope,
            permissions=payload.permissions,
            rationale=streamed.rationale,
        )
    if isinstance(payload, FileSharingRequestPayload):
        return LatchkeyFileSharingPermissionRequestEvent(
            timestamp=_now_iso(),
            type=EventType("file_sharing_permission_request"),
            event_id=EventId(streamed.request_id),
            source=EventSource(REQUESTS_EVENT_SOURCE_NAME),
            agent_id=streamed.agent_id,
            request_type=str(RequestType.FILE_SHARING_PERMISSION),
            is_user_requested=False,
            permissions_target_path=streamed.target,
            path=payload.path,
            access=str(payload.access),
            rationale=streamed.rationale,
        )
    assert_never(payload)


class PermissionRequestsConsumer(MutableModel):
    """Long-running thread that pumps gateway-side permission requests into the inbox.

    The thread is launched via :meth:`start` (which adds it to a
    :class:`ConcurrencyGroup` so process-wide shutdown tears it down)
    and stops on :meth:`stop` or on group exit. ``on_request`` is
    invoked from the consumer thread for every fresh request -- the
    desktop client's callback owns whatever side effects (inbox
    update, SSE wakeup) belong with each one.
    """

    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True,
        description="HTTP client used to talk to the gateway's bundled extension endpoints.",
    )
    on_request: Callable[[RequestEvent], None] = Field(
        description=(
            "Callback invoked from the consumer thread for each streamed permission request "
            "after translation into the inbox event shape. Receives either a "
            ":class:`LatchkeyPredefinedPermissionRequestEvent` (for ``type=predefined``) or a "
            ":class:`LatchkeyFileSharingPermissionRequestEvent` (for ``type=file-sharing``)."
        ),
    )

    # Co-ordination state; held as private attributes so pydantic does
    # not try to serialize a :class:`threading.Event` or a live thread
    # object.
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: ObservableThread | None = PrivateAttr(default=None)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Spawn the consumer thread under ``concurrency_group``. Idempotent."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = concurrency_group.start_new_thread(
            target=self._run,
            name="latchkey-permission-requests-consumer",
            daemon=True,
            # The consumer thread is intentionally fire-and-forget: it
            # is the request inbox's source of truth and a crash inside
            # it has already been logged. ``is_checked=False`` keeps
            # the group's ``__exit__`` from re-raising on this thread.
            is_checked=False,
        )

    def stop(self) -> None:
        """Signal the consumer thread to exit. Returns immediately.

        The follow stream uses a finite read timeout (see
        :data:`~imbue.minds.desktop_client.latchkey.gateway_client._FOLLOW_READ_TIMEOUT`)
        so the consumer thread wakes up at least every couple of seconds
        and notices the stop event between reconnect attempts. Worst-case
        shutdown delay is one read-timeout interval.

        Safe to call concurrently with :meth:`start` / ``_run`` /
        another :meth:`stop`; the event set is idempotent.
        """
        self._stop_event.set()

    def _run(self) -> None:
        """Consumer-thread main loop: stream, reconnect on failure, exit on stop."""
        delay = _RECONNECT_MIN_DELAY_SECONDS
        while not self._stop_event.is_set():
            try:
                for streamed in self.gateway_client.iter_permission_requests():
                    if self._stop_event.is_set():
                        return
                    try:
                        event = streamed_request_to_event(streamed)
                        self.on_request(event)
                    except Exception as e:
                        # A single request the consumer cannot process must never
                        # take down this thread: it is the request inbox's source
                        # of truth, so an uncaught error here silently stops EVERY
                        # future permission request -- for any agent or service --
                        # from reaching the UI until restart (and re-crashes on the
                        # same record on each reconnect). Log the failure with its
                        # traceback and skip just this request. The gateway
                        # validates request bodies up front (see
                        # permission_requests.mjs), so this broad catch is a
                        # defense-in-depth backstop for malformed/legacy records,
                        # not the primary guard.
                        logger.opt(exception=e).error(
                            "Skipping a permission request the consumer could not process "
                            "(request_id={}, agent_id={}): {}",
                            streamed.request_id,
                            streamed.agent_id,
                            e,
                        )
                        continue
                    # Reset backoff after a successful delivery.
                    delay = _RECONNECT_MIN_DELAY_SECONDS
            except LatchkeyGatewayClientError as e:
                logger.warning(
                    "permission-requests stream dropped ({}); reconnecting in {:.1f}s",
                    e,
                    delay,
                )
                # Real error -- escalate the backoff so a dead gateway
                # doesn't get hammered.
                if self._stop_event.wait(timeout=delay):
                    return
                delay = min(delay * _RECONNECT_DELAY_GROWTH, _RECONNECT_MAX_DELAY_SECONDS)
            else:
                # Clean close from the gateway side OR a read-timeout
                # idle reconnect (iter_permission_requests treats
                # httpx.ReadTimeout as a clean close, see its docstring).
                # Reset the backoff -- the previous attempt succeeded at
                # the protocol level, no failure to back off from -- but
                # still pace ourselves with the min delay so a gateway
                # that immediately closes every connection (instead of
                # holding the stream open) can't induce a tight
                # reconnect loop.
                delay = _RECONNECT_MIN_DELAY_SECONDS
                if self._stop_event.wait(timeout=delay):
                    return
