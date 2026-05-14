"""Unit tests for :class:`PermissionRequestsConsumer` and its event translator."""

import json
import threading
import time
from typing import Final

import httpx

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import streamed_request_to_event
from imbue.minds.desktop_client.request_events import LatchkeyPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestType

_POLL_TIMEOUT_SECONDS: Final[float] = 2.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02


def test_streamed_request_to_event_maps_fields_and_uses_request_id_as_event_id() -> None:
    """The translator reuses ``request_id`` as ``event_id`` so the inbox keys join cleanly."""
    streamed = StreamedPermissionRequest(
        request_id="abc123",
        agent_id="agent-9",
        service_name="slack",
        rationale="needs to read messages",
    )
    event = streamed_request_to_event(streamed)
    assert isinstance(event, LatchkeyPermissionRequestEvent)
    assert str(event.event_id) == "abc123"
    assert event.agent_id == "agent-9"
    assert event.service_name == "slack"
    assert event.rationale == "needs to read messages"
    assert event.request_type == str(RequestType.LATCHKEY_PERMISSION)


def _wait_until(predicate, timeout: float = _POLL_TIMEOUT_SECONDS) -> bool:
    """Spin-wait until ``predicate`` is truthy or ``timeout`` elapses. Returns the final value."""
    deadline = time.monotonic() + timeout
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        waiter.wait(timeout=_POLL_INTERVAL_SECONDS)
    return predicate()


def test_consumer_dispatches_each_streamed_request_to_on_request() -> None:
    """Every streamed JSONL record reaches the registered callback as a parsed event."""
    payload = b"".join(
        json.dumps(item).encode("utf-8") + b"\n"
        for item in (
            {"request_id": "r1", "agent_id": "a1", "service_name": "slack", "rationale": "x"},
            {"request_id": "r2", "agent_id": "a2", "service_name": "github", "rationale": "y"},
        )
    )

    # Bound the number of stream responses the transport hands out so
    # the consumer's reconnect loop eventually idles (with stop()
    # taking effect on the next short sleep). A single 200 with the
    # two-line payload is enough: when the transport closes, the
    # consumer goes through one reconnect-backoff iteration and we
    # signal stop().
    delivered: list[LatchkeyPermissionRequestEvent] = []
    lock = threading.Lock()

    def _on_request(event: LatchkeyPermissionRequestEvent) -> None:
        with lock:
            delivered.append(event)

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient(
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
        transport=httpx.MockTransport(_handler),
    )
    consumer = PermissionRequestsConsumer(gateway_client=client, on_request=_on_request)
    cg = ConcurrencyGroup(name="permission-requests-consumer-test")
    with cg:
        consumer.start(cg)
        try:
            assert _wait_until(lambda: len(delivered) >= 2)
        finally:
            consumer.stop()
    assert {str(e.event_id) for e in delivered} == {"r1", "r2"}
    assert {e.service_name for e in delivered} == {"slack", "github"}
