"""Unit tests for :class:`LatchkeyGatewayClient`.

A :class:`httpx.MockTransport` is injected via the ``transport`` field
so each test can stub the gateway's HTTP layer without any real I/O.
"""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> LatchkeyGatewayClient:
    """Build a :class:`LatchkeyGatewayClient` whose transport is the handler."""
    return LatchkeyGatewayClient(
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
        transport=httpx.MockTransport(handler),
    )


def test_set_permission_rule_sends_expected_headers_body_and_url() -> None:
    """``set_permission_rule`` POSTs with admin headers and the right query params."""
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("X-Latchkey-Gateway-Password")
        captured["override"] = request.headers.get("X-Latchkey-Gateway-Permissions-Override")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"slack-api": ["any"]})

    client = _build_client(_handler)
    client.set_permission_rule(
        permissions_file_path=Path("/perms/host-1/latchkey_permissions.json"),
        rule_key="slack-api",
        granted_permissions=["any"],
    )
    assert captured["method"] == "POST"
    url = str(captured["url"])
    assert url.startswith("http://gateway.invalid:1989/permissions/rules")
    assert "rule_key=slack-api" in url
    assert "latchkey_permissions.json" in url
    assert captured["auth"] == "hunter2"
    assert captured["override"] == "admin-jwt-token"
    assert captured["body"] == ["any"]


def test_set_permission_rule_raises_on_non_2xx() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"error": "outside root"})

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError) as exc_info:
        client.set_permission_rule(
            permissions_file_path=Path("/etc/passwd"),
            rule_key="any",
            granted_permissions=["any"],
        )
    assert "403" in str(exc_info.value)
    assert "outside root" in str(exc_info.value)


def test_delete_permission_request_tolerates_404() -> None:
    """Deletes that race a concurrent grant/deny succeed silently."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/permission-requests/evt-abc"
        return httpx.Response(404, json={"error": "gone"})

    client = _build_client(_handler)
    # No assertion -- this must not raise.
    client.delete_permission_request("evt-abc")


def test_delete_permission_request_raises_on_other_4xx() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": "no auth"})

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError) as exc_info:
        client.delete_permission_request("evt-abc")
    assert "401" in str(exc_info.value)


def test_iter_permission_requests_parses_jsonl_stream() -> None:
    """``iter_permission_requests`` decodes JSONL into :class:`StreamedPermissionRequest`."""
    requests_payload = [
        {
            "request_id": "abc",
            "agent_id": "a1",
            "scope": "slack-api",
            "permissions": ["slack-read-all"],
            "rationale": "why",
        },
        {
            "request_id": "def",
            "agent_id": "a2",
            "scope": "github-rest-api",
            "permissions": [],
            "rationale": "test",
        },
    ]
    body = "".join(json.dumps(item) + "\n" for item in requests_payload).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Latchkey-Gateway-Password"] == "hunter2"
        assert request.headers["X-Latchkey-Gateway-Permissions-Override"] == "admin-jwt-token"
        assert "follow=true" in str(request.url)
        return httpx.Response(200, content=body, headers={"Content-Type": "application/x-ndjson"})

    client = _build_client(_handler)
    items = list(client.iter_permission_requests())
    assert items == [
        StreamedPermissionRequest(
            request_id="abc",
            agent_id="a1",
            scope="slack-api",
            permissions=("slack-read-all",),
            rationale="why",
        ),
        StreamedPermissionRequest(
            request_id="def",
            agent_id="a2",
            scope="github-rest-api",
            permissions=(),
            rationale="test",
        ),
    ]


def test_iter_permission_requests_skips_malformed_lines() -> None:
    """Garbage lines (non-JSON or wrong shape) are dropped with a warning, not raised."""
    payload = (
        b'not valid json\n'
        b'{"agent_id": "a1"}\n'
        b'{"request_id":"x","agent_id":"a2","scope":"s-api","permissions":["p"],"rationale":"r"}\n'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload)

    client = _build_client(_handler)
    items = list(client.iter_permission_requests())
    assert items == [
        StreamedPermissionRequest(
            request_id="x",
            agent_id="a2",
            scope="s-api",
            permissions=("p",),
            rationale="r",
        ),
    ]


def test_get_available_services_returns_parsed_payload() -> None:
    """``get_available_services`` GETs the catalog endpoint and returns the parsed JSON object."""
    payload = {
        "slack": {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": ["slack-read-all"],
        },
    }

    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["auth"] = request.headers.get("X-Latchkey-Gateway-Password")
        captured["override"] = request.headers.get("X-Latchkey-Gateway-Permissions-Override")
        return httpx.Response(200, json=payload)

    client = _build_client(_handler)
    result = client.get_available_services()

    assert result == payload
    assert captured["method"] == "GET"
    assert captured["path"] == "/permissions/available"
    assert captured["auth"] == "hunter2"
    assert captured["override"] == "admin-jwt-token"


def test_get_available_services_raises_on_non_2xx() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, content=b"boom")

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_available_services()


def test_get_available_services_raises_on_non_object_body() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=[1, 2, 3])

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_available_services()


def test_get_granted_permissions_unions_matching_scopes() -> None:
    """The reader collects permission names across every rule whose key is in ``scopes``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/permissions"
        assert "path=" in str(request.url)
        return httpx.Response(
            200,
            json={
                "rules": [
                    {"slack-api": ["slack-read-all", "slack-write-messages"]},
                    {"github-rest-api": ["github-read-all"]},
                    {"slack-api": ["any"]},
                ],
            },
        )

    client = _build_client(_handler)
    granted = client.get_granted_permissions_for_scopes(
        Path("/perms/host-1/latchkey_permissions.json"),
        scopes=["slack-api"],
    )
    assert granted == frozenset({"slack-read-all", "slack-write-messages", "any"})


def test_get_granted_permissions_returns_empty_on_404() -> None:
    """A missing permissions file is treated as 'no rules', not an error."""

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"error": "not found"})

    client = _build_client(_handler)
    granted = client.get_granted_permissions_for_scopes(
        Path("/perms/host-1/latchkey_permissions.json"),
        scopes=["slack-api"],
    )
    assert granted == frozenset()


def test_get_granted_permissions_raises_on_other_4xx() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"error": "outside root"})

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_granted_permissions_for_scopes(
            Path("/etc/passwd"),
            scopes=["any"],
        )


def test_iter_permission_requests_raises_on_http_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, content=b"explode")

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        list(client.iter_permission_requests())
