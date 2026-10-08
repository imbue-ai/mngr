"""Unit tests for :class:`PermissionRequestsConsumer`."""

import json
import threading
import time
from typing import Final

import httpx

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.permission_requests_consumer import PermissionRequestsConsumer

_POLL_TIMEOUT_SECONDS: Final[float] = 2.0
# For a wait that spans several reconnects: the consumer paces each one by a
# second after a clean close, so two reconnects alone take the default deadline.
_RECONNECTS_TIMEOUT_SECONDS: Final[float] = 10.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02


def _wait_until(predicate, timeout: float = _POLL_TIMEOUT_SECONDS) -> bool:
    """Spin-wait until ``predicate`` is truthy or ``timeout`` elapses. Returns the final value."""
    deadline = time.monotonic() + timeout
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        waiter.wait(timeout=_POLL_INTERVAL_SECONDS)
    return predicate()


def test_consumer_signals_once_per_request_and_dedupes_redeliveries() -> None:
    """Each fresh request fires the signal exactly once, across reconnect re-emissions."""
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
    signal_count = 0
    lock = threading.Lock()
    connections = 0

    def _on_new_request() -> None:
        nonlocal signal_count
        with lock:
            signal_count += 1

    def _handler(request: httpx.Request) -> httpx.Response:
        # Every reconnect re-emits the full pending set, as the gateway does.
        nonlocal connections
        connections += 1
        del request
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
    )
    consumer = PermissionRequestsConsumer(gateway_client=client, on_change=_on_new_request)
    cg = ConcurrencyGroup(name="permission-requests-consumer-test")
    with cg:
        consumer.start(cg)
        try:
            assert _wait_until(lambda: signal_count >= 2 and connections >= 2)
        finally:
            consumer.stop()
    # Two fresh requests, re-emitted on later reconnects: still two signals.
    assert signal_count == 2


def test_consumer_survives_a_signal_error_and_keeps_processing() -> None:
    """A raising signal callback must not take the consumer thread down."""
    payload = b"".join(
        json.dumps(item).encode("utf-8") + b"\n"
        for item in (
            {
                "request_id": "boom",
                "agent_id": "a1",
                "rationale": "x",
                "request_type": "predefined",
                "payload": {"scope": "slack-api", "permissions": []},
                "target": "/tmp/permissions.json",
                "effect": {"rules": []},
            },
            {
                "request_id": "fine",
                "agent_id": "a2",
                "rationale": "y",
                "request_type": "predefined",
                "payload": {"scope": "github-api", "permissions": []},
                "target": "/tmp/permissions.json",
                "effect": {"rules": []},
            },
        )
    )
    seen: list[int] = []
    lock = threading.Lock()

    def _on_new_request() -> None:
        with lock:
            seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("first signal exploded")

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
    )
    consumer = PermissionRequestsConsumer(gateway_client=client, on_change=_on_new_request)
    cg = ConcurrencyGroup(name="permission-requests-consumer-test")
    with cg:
        consumer.start(cg)
        try:
            assert _wait_until(lambda: len(seen) >= 2)
        finally:
            consumer.stop()


def test_consumer_signals_a_request_that_stopped_being_pending_and_forgets_having_seen_it() -> None:
    """A deletion wakes the surfaces like a new request does, and the same id filed again is news again."""
    record = {
        "request_id": "r-again",
        "agent_id": "a1",
        "rationale": "x",
        "request_type": "accounts",
        "payload": {},
        "target": "/tmp/permissions.json",
        "effect": {"rules": []},
    }
    deletion = {"event": "deleted", "request_id": "r-again"}
    # The first connection sees the request filed, withdrawn and filed again;
    # every reconnect re-emits the request, which is then old news.
    first_connection = b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in (record, deletion, record))
    reconnect = json.dumps(record).encode("utf-8") + b"\n"
    signal_count = 0
    connections = 0
    lock = threading.Lock()

    def _on_change() -> None:
        nonlocal signal_count
        with lock:
            signal_count += 1

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal connections
        del request
        with lock:
            connections += 1
            content = first_connection if connections == 1 else reconnect
        return httpx.Response(200, content=content, headers={"Content-Type": "application/x-ndjson"})

    client = LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(_handler),
        base_url="http://gateway.invalid:1989",
        password="p",
        admin_jwt="jwt",
    )
    consumer = PermissionRequestsConsumer(gateway_client=client, on_change=_on_change)
    cg = ConcurrencyGroup(name="permission-requests-consumer-deletion-test")
    with cg:
        consumer.start(cg)
        try:
            assert _wait_until(lambda: connections >= 3, timeout=_RECONNECTS_TIMEOUT_SECONDS)
        finally:
            consumer.stop()
    # Filed, withdrawn, filed again: three signals, and none for the re-emissions.
    assert signal_count == 3
