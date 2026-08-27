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
    consumer = PermissionRequestsConsumer(gateway_client=client, on_new_request=_on_new_request)
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
    consumer = PermissionRequestsConsumer(gateway_client=client, on_new_request=_on_new_request)
    cg = ConcurrencyGroup(name="permission-requests-consumer-test")
    with cg:
        consumer.start(cg)
        try:
            assert _wait_until(lambda: len(seen) >= 2)
        finally:
            consumer.stop()
