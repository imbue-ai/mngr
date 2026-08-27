"""Background change signal for the gateway's ``permission-requests`` stream.

Spawned at desktop-client startup, owns a daemon thread that holds a
long-lived ``GET /permission-requests?follow=true`` connection open
against the shared latchkey gateway and fires ``on_new_request`` the
first time each pending request is seen. That is its whole job: pending
state itself is read on demand from the gateway (see
``latchkey/pending_requests.py``), so the signal only wakes the chrome
SSE -- every surface then re-reads.

The stream re-emits every still-pending request on each reconnect (and
reconnects every couple of seconds when idle), so first-sight dedup by
``request_id`` keeps the signal quiet when nothing changed. The seen-set
is thread-local to the consumer and append-only.
"""

import threading
from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError

# Backoff bounds for the reconnect loop. The lower bound keeps the
# consumer responsive when the gateway is just slow to start; the upper
# bound prevents pathological busy-looping if the gateway dies and is
# never restarted.
_RECONNECT_MIN_DELAY_SECONDS: Final[float] = 1.0
_RECONNECT_MAX_DELAY_SECONDS: Final[float] = 30.0
_RECONNECT_DELAY_GROWTH: Final[float] = 2.0


class PermissionRequestsConsumer(MutableModel):
    """Long-running thread that signals the arrival of fresh permission requests.

    The thread is launched via :meth:`start` (which adds it to a
    :class:`ConcurrencyGroup` so process-wide shutdown tears it down)
    and stops on :meth:`stop` or on group exit.
    """

    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True,
        description="HTTP client used to talk to the gateway's bundled extension endpoints.",
    )
    on_new_request: Callable[[], None] = Field(
        description=(
            "Invoked from the consumer thread the first time each pending request is seen; "
            "wakes whatever re-reads pending state (in production, the chrome SSE)."
        ),
    )

    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: ObservableThread | None = PrivateAttr(default=None)
    _seen_request_ids: set[str] = PrivateAttr(default_factory=set)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Spawn the consumer thread under ``concurrency_group``. Idempotent."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = concurrency_group.start_new_thread(
            target=self._run,
            name="latchkey-permission-requests-consumer",
            daemon=True,
            # Fire-and-forget: a crash inside it has already been logged, and
            # is_checked=False keeps the group's __exit__ from re-raising.
            is_checked=False,
        )

    def stop(self) -> None:
        """Signal the consumer thread to exit. Returns immediately.

        The follow stream uses a finite read timeout (see
        :data:`~imbue.minds.desktop_client.latchkey.gateway_client._FOLLOW_READ_TIMEOUT`)
        so the consumer thread wakes up at least every couple of seconds
        and notices the stop event between reconnect attempts.
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
                    if streamed.request_id in self._seen_request_ids:
                        continue
                    self._seen_request_ids.add(streamed.request_id)
                    logger.info(
                        "Streamed permission request for agent {} (request_type={}, request_id={})",
                        streamed.agent_id,
                        streamed.request_type,
                        streamed.request_id,
                    )
                    try:
                        self.on_new_request()
                    except (OSError, RuntimeError) as e:
                        logger.opt(exception=e).error("permission-request change signal failed: {}", e)
                    delay = _RECONNECT_MIN_DELAY_SECONDS
            except LatchkeyGatewayClientError as e:
                logger.warning(
                    "permission-requests stream dropped ({}); reconnecting in {:.1f}s",
                    e,
                    delay,
                )
                if self._stop_event.wait(timeout=delay):
                    return
                delay = min(delay * _RECONNECT_DELAY_GROWTH, _RECONNECT_MAX_DELAY_SECONDS)
            else:
                # Clean close or idle read-timeout reconnect: pace with the min
                # delay so an immediately-closing gateway can't induce a tight
                # reconnect loop.
                delay = _RECONNECT_MIN_DELAY_SECONDS
                if self._stop_event.wait(timeout=delay):
                    return
