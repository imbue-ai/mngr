"""Unit tests for :class:`LatchkeyGatewayClient`.

A :class:`httpx.MockTransport` is injected via the ``transport`` field
so each test can stub the gateway's HTTP layer without any real I/O.
"""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PredefinedRequestPayload


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> LatchkeyGatewayClient:
    """Build a pre-initialized :class:`LatchkeyGatewayClient` whose transport is the handler.

    The credential attrs are private and populated in production by
    :meth:`LatchkeyGatewayClient.ensure_initialized` reading from a
    :class:`Latchkey`. Tests skip that lazy-init path entirely by
    pre-populating the credentials directly on the instance.
    """
    client = LatchkeyGatewayClient.from_credentials(
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
        transport=httpx.MockTransport(handler),
    )
    return client


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
            "rationale": "why",
            "request_type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
            "target": "/tmp/permissions.json",
            "effect": {"rules": [{"slack-api": ["slack-read-all"]}]},
        },
        {
            "request_id": "def",
            "agent_id": "a2",
            "rationale": "test",
            "request_type": "file-sharing",
            "payload": {"path": "/home/user/file.txt", "access": "READ"},
            "target": "/tmp/permissions.json",
            "effect": {"rules": [{"latchkey-self": ["minds-file-server-cafef00d"]}]},
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
    assert [item.request_id for item in items] == ["abc", "def"]
    assert items[0].request_type == "predefined"
    predefined_payload = items[0].payload
    assert isinstance(predefined_payload, PredefinedRequestPayload)
    assert predefined_payload.scope == "slack-api"
    assert predefined_payload.permissions == ("slack-read-all",)
    assert items[1].request_type == "file-sharing"
    file_sharing_payload = items[1].payload
    assert isinstance(file_sharing_payload, FileSharingRequestPayload)
    assert file_sharing_payload.path == "/home/user/file.txt"
    assert str(file_sharing_payload.access) == "READ"


def test_iter_permission_requests_skips_malformed_lines() -> None:
    """Garbage lines (non-JSON or wrong shape) are dropped with a warning, not raised."""
    valid_record = {
        "request_id": "x",
        "agent_id": "a2",
        "rationale": "r",
        "request_type": "predefined",
        "payload": {"scope": "s-api", "permissions": ["p"]},
        "target": "/tmp/permissions.json",
        "effect": {"rules": [{"s-api": ["p"]}]},
    }
    payload = b'not valid json\n{"agent_id": "a1"}\n' + json.dumps(valid_record).encode("utf-8") + b"\n"

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=payload)

    client = _build_client(_handler)
    items = list(client.iter_permission_requests())
    assert [item.request_id for item in items] == ["x"]
    assert items[0].request_type == "predefined"
    predefined_payload = items[0].payload
    assert isinstance(predefined_payload, PredefinedRequestPayload)
    assert predefined_payload.scope == "s-api"


def test_approve_permission_request_posts_through_gateway() -> None:
    """``approve_permission_request`` POSTs to /permission-requests/approve/<id> and tolerates the gateway's 200."""
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"request_id": "evt-abc", "target": "/tmp/p.json", "applied": {}})

    client = _build_client(_handler)
    # Must not raise.
    client.approve_permission_request("evt-abc")
    assert captured == {"method": "POST", "path": "/permission-requests/approve/evt-abc"}


def test_approve_permission_request_sends_no_body_without_override() -> None:
    """Without an override path the approve POST carries an empty body (gateway uses the precomputed effect)."""
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"request_id": "evt-abc"})

    client = _build_client(_handler)
    client.approve_permission_request("evt-abc")
    assert captured["content"] == b""


def test_approve_permission_request_sends_override_path_body() -> None:
    """An override path is sent as a ``{"path": ...}`` JSON body so the gateway recomputes the effect."""
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"request_id": "evt-abc"})

    client = _build_client(_handler)
    client.approve_permission_request("evt-abc", override_path="/Users/glenn/Documents/Shared")
    sent_body = captured["content"]
    assert isinstance(sent_body, bytes)
    assert json.loads(sent_body) == {"path": "/Users/glenn/Documents/Shared"}
    assert captured["content_type"] == "application/json"


def test_approve_permission_request_raises_on_4xx() -> None:
    """Unlike ``delete``, ``approve`` does *not* swallow 404 / 4xx: a missing grant is a hard error."""

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"error": "not found"})

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError) as exc_info:
        client.approve_permission_request("evt-abc")
    assert "404" in str(exc_info.value)


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


# -- Connect-level self-healing -------------------------------------------


def test_invalidate_initialization_clears_cached_state() -> None:
    """After ``invalidate_initialization`` the client behaves as if never initialized.

    A ``from_credentials``-built client has no :class:`Latchkey` to
    re-derive from, so post-invalidate HTTP calls surface as
    ``LatchkeyGatewayClientNotInitializedError`` (a subclass of
    ``LatchkeyGatewayClientError``). The production code path always
    builds via :meth:`from_latchkey` and re-resolves cleanly; this
    test pins the contract that invalidation actually clears state.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={})

    client = _build_client(_handler)
    # First call works against the cached credentials.
    client.delete_permission_request("evt-abc")

    client.invalidate_initialization()

    # No ``_latchkey`` to re-resolve from, so the next call refuses to
    # build a URL at all.
    with pytest.raises(LatchkeyGatewayClientError):
        client.delete_permission_request("evt-abc")


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timeout"),
    ],
)
def test_one_shot_methods_invalidate_on_connect_level_errors(transport_error: httpx.HTTPError) -> None:
    """Connect-level transport failures clear the cached gateway URL.

    This is the load-bearing self-heal that lets the desktop client
    recover from a stale cached port (typically observed when the
    supervisor restarted on a new port after the gateway client
    cached the previous one).
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        raise transport_error

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_granted_permissions_for_scopes(Path("/tmp/p.json"), ("slack-api",))
    # ``_require_base_url`` now raises ``LatchkeyGatewayClientNotInitializedError``
    # (a subclass of ``LatchkeyGatewayClientError``) because the cache
    # was cleared by the connect-error handler.
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_granted_permissions_for_scopes(Path("/tmp/p.json"), ("slack-api",))


def test_non_connect_transport_errors_do_not_invalidate() -> None:
    """Non-connect transport failures (e.g. mid-response ``ReadError``) propagate without invalidating.

    A read-level failure indicates a problem mid-stream rather than a
    stale local cache. Clearing state on every transient transport
    hiccup would force an unnecessary supervisor-record re-read; the
    cached URL is still very likely correct.
    """

    call_count = {"value": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        call_count["value"] += 1
        raise httpx.ReadError("server hung up mid-response")

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_granted_permissions_for_scopes(Path("/tmp/p.json"), ("slack-api",))
    # The cache was *not* cleared, so the second call hits the
    # transport again (instead of failing fast on the missing
    # ``base_url``).
    with pytest.raises(LatchkeyGatewayClientError):
        client.get_granted_permissions_for_scopes(Path("/tmp/p.json"), ("slack-api",))
    assert call_count["value"] == 2


def test_iter_permission_requests_invalidates_on_connect_error() -> None:
    """The streaming path also self-heals on connect-level failures.

    This is the one consumers of the stream care most about: the
    background reconnect loop in ``PermissionRequestsConsumer`` will
    call ``iter_permission_requests`` again after our exception, and
    the cleared state means the next call re-resolves the gateway URL
    from the supervisor record instead of pounding the stale port.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    client = _build_client(_handler)
    with pytest.raises(LatchkeyGatewayClientError):
        list(client.iter_permission_requests())
    # State cleared -- next call cannot build a URL.
    with pytest.raises(LatchkeyGatewayClientError):
        list(client.iter_permission_requests())
