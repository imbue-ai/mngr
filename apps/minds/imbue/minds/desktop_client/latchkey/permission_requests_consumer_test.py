"""Unit tests for :class:`PermissionRequestsConsumer` and its event translator."""

import json
import threading
import time
from typing import Final

import httpx

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import PermissionEffect
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import streamed_request_to_event
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import LatchkeyPredefinedPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestType

_POLL_TIMEOUT_SECONDS: Final[float] = 2.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02


def _make_streamed_predefined(
    request_id: str = "abc123",
    agent_id: str = "agent-9",
    scope: str = "slack-api",
    permissions: tuple[str, ...] = ("slack-read-all",),
    rationale: str = "needs to read messages",
) -> StreamedPermissionRequest:
    return StreamedPermissionRequest(
        request_id=request_id,
        agent_id=agent_id,
        rationale=rationale,
        request_type="predefined",
        payload=PredefinedRequestPayload(scope=scope, permissions=permissions),
        target="/tmp/permissions.json",
        effect=PermissionEffect(rules=({scope: permissions},)),
    )


def _make_streamed_file_sharing(
    request_id: str = "def456",
    agent_id: str = "agent-7",
    path: str = "/home/user/data.txt",
    access: str = "READ",
    rationale: str = "needs data",
) -> StreamedPermissionRequest:
    return StreamedPermissionRequest(
        request_id=request_id,
        agent_id=agent_id,
        rationale=rationale,
        request_type="file-sharing",
        payload=FileSharingRequestPayload.model_validate({"path": path, "access": access}),
        target="/tmp/permissions.json",
        effect=PermissionEffect(rules=({"latchkey-self": ("minds-file-server-deadbeef",)},)),
    )


def test_streamed_request_to_event_maps_predefined_fields() -> None:
    """Predefined-type streamed records translate to LatchkeyPredefinedPermissionRequestEvent."""
    event = streamed_request_to_event(_make_streamed_predefined())
    assert isinstance(event, LatchkeyPredefinedPermissionRequestEvent)
    assert str(event.event_id) == "abc123"
    assert event.agent_id == "agent-9"
    assert event.scope == "slack-api"
    assert event.permissions == ("slack-read-all",)
    assert event.rationale == "needs to read messages"
    assert event.request_type == str(RequestType.LATCHKEY_PERMISSION)
    # ``target`` (the agent's opaque permissions handle) is carried through so
    # the inbox handler can recover a missing canonical host permissions file.
    assert event.permissions_target_path == "/tmp/permissions.json"


def test_streamed_request_to_event_maps_file_sharing_fields() -> None:
    """File-sharing-type streamed records translate to LatchkeyFileSharingPermissionRequestEvent."""
    event = streamed_request_to_event(_make_streamed_file_sharing(access="WRITE"))
    assert isinstance(event, LatchkeyFileSharingPermissionRequestEvent)
    assert str(event.event_id) == "def456"
    assert event.agent_id == "agent-7"
    assert event.path == "/home/user/data.txt"
    assert event.access == "WRITE"
    assert event.rationale == "needs data"
    assert event.request_type == str(RequestType.FILE_SHARING_PERMISSION)
    assert event.permissions_target_path == "/tmp/permissions.json"


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
            {
                "request_id": "r1",
                "agent_id": "a1",
                "rationale": "x",
                "request_type": "predefined",
                "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
                "target": "/tmp/permissions.json",
                "effect": {"rules": [{"slack-api": ["slack-read-all"]}]},
            },
            {
                "request_id": "r2",
                "agent_id": "a2",
                "rationale": "y",
                "request_type": "file-sharing",
                "payload": {"path": "/home/user/log.txt", "access": "READ"},
                "target": "/tmp/permissions.json",
                "effect": {"rules": [{"latchkey-self": ["minds-file-server-cafef00d"]}]},
            },
        )
    )

    # Bound the number of stream responses the transport hands out so
    # the consumer's reconnect loop eventually idles (with stop()
    # taking effect on the next short sleep). A single 200 with the
    # two-line payload is enough: when the transport closes, the
    # consumer goes through one reconnect-backoff iteration and we
    # signal stop().
    delivered: list[RequestEvent] = []
    lock = threading.Lock()

    def _on_request(event: RequestEvent) -> None:
        with lock:
            delivered.append(event)

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
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
    predefined = next(e for e in delivered if isinstance(e, LatchkeyPredefinedPermissionRequestEvent))
    file_sharing = next(e for e in delivered if isinstance(e, LatchkeyFileSharingPermissionRequestEvent))
    assert predefined.scope == "slack-api"
    assert file_sharing.path == "/home/user/log.txt"


def test_consumer_survives_an_on_request_error_and_keeps_processing() -> None:
    """A callback that raises on one request must not kill the consumer thread.

    The consumer thread is the inbox's source of truth: if one bad request
    propagated out of the loop the thread would die and every *subsequent*
    permission request would stop reaching the UI. The loop catches the error,
    logs it, and moves on to the next streamed record.
    """
    payload = b"".join(
        json.dumps(item).encode("utf-8") + b"\n"
        for item in (
            {
                "request_id": "bad",
                "agent_id": "a1",
                "rationale": "x",
                "request_type": "predefined",
                "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
                "target": "/tmp/permissions.json",
                "effect": {"rules": [{"slack-api": ["slack-read-all"]}]},
            },
            {
                "request_id": "good",
                "agent_id": "a2",
                "rationale": "y",
                "request_type": "file-sharing",
                "payload": {"path": "/home/user/log.txt", "access": "READ"},
                "target": "/tmp/permissions.json",
                "effect": {"rules": [{"latchkey-self": ["minds-file-server-cafef00d"]}]},
            },
        )
    )

    delivered: list[str] = []
    lock = threading.Lock()

    def _on_request(event: RequestEvent) -> None:
        with lock:
            delivered.append(str(event.event_id))
        # Simulate a per-request processing failure on the first record. The
        # loop must catch it, log it, and still reach the second record.
        if str(event.event_id) == "bad":
            raise RuntimeError("boom")

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
    )
    consumer = PermissionRequestsConsumer(gateway_client=client, on_request=_on_request)
    cg = ConcurrencyGroup(name="permission-requests-consumer-error-test")
    with cg:
        consumer.start(cg)
        try:
            # 'good' is only reached if the loop survived the raise on 'bad'.
            assert _wait_until(lambda: "good" in delivered)
        finally:
            consumer.stop()
    assert "bad" in delivered and "good" in delivered
