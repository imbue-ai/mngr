"""Unit tests for :class:`PermissionRequestsConsumer` and its event translator."""

import json
import threading

import httpx
import pytest

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

# Generous overall ceiling for the streamed events to reach the
# callback. The first stream response delivers both records almost
# immediately, but the consumer runs on a background thread under a
# reconnect/backoff loop, so on a heavily loaded CI box the scheduler
# may not run it for a while. We wait on a real ``threading.Event``
# (set by the callback once enough events arrive) rather than sleeping,
# so we return the instant the condition is met and only ever approach
# this ceiling when something is genuinely wrong.
_DELIVERY_TIMEOUT_SECONDS: float = 30.0


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


@pytest.mark.flaky
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

    # The transport returns the same two-line 200 on every connect, so
    # the consumer's reconnect loop re-delivers ``r1``/``r2`` on each
    # iteration. Redelivery is expected and harmless (the inbox is keyed
    # by ``event_id``), which is exactly why the final assertion is over
    # the *set* of delivered ids rather than the exact sequence/count.
    delivered: list[RequestEvent] = []
    lock = threading.Lock()
    # Set once the first batch (both records) has been delivered, so the
    # test can wake immediately instead of polling.
    both_delivered = threading.Event()

    def _on_request(event: RequestEvent) -> None:
        with lock:
            delivered.append(event)
            if len(delivered) >= 2:
                both_delivered.set()

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
            assert both_delivered.wait(timeout=_DELIVERY_TIMEOUT_SECONDS), (
                f"consumer did not deliver both streamed requests within {_DELIVERY_TIMEOUT_SECONDS}s; "
                f"got {[str(e.event_id) for e in delivered]}"
            )
        finally:
            consumer.stop()
    # Redelivery means ``delivered`` may hold more than two entries; the
    # contract is that *every* streamed id reached the callback, so we
    # assert on the de-duplicated set.
    assert {str(e.event_id) for e in delivered} == {"r1", "r2"}
    predefined = next(e for e in delivered if isinstance(e, LatchkeyPredefinedPermissionRequestEvent))
    file_sharing = next(e for e in delivered if isinstance(e, LatchkeyFileSharingPermissionRequestEvent))
    assert predefined.scope == "slack-api"
    assert file_sharing.path == "/home/user/log.txt"
