"""Tests for the connector HTTP client.

We mount an httpx MockTransport on the underlying transport so the calls
never go to the network; this isolates the tests from connector availability
and makes them deterministic.
"""

import json as _json

import httpx
import pytest
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.client import _auth_policy_to_connector_body
from imbue.mngr_imbue_cloud.client import _parse_auth_policy
from imbue.mngr_imbue_cloud.data_types import AuthPolicy
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.errors import ImbueCloudConnectorError
from imbue.mngr_imbue_cloud.errors import ImbueCloudHostNameConflictError
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError


def _make_client(handler) -> tuple[ImbueCloudConnectorClient, httpx.MockTransport]:
    transport = httpx.MockTransport(handler)

    # Patch httpx module-level functions to use the transport for the duration of the test.
    # The client uses module-level httpx.* calls; intercept them via monkeypatch in tests.
    return ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com")), transport


def test_lease_host_503_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no match"})

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudLeaseUnavailableError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", host_name="my-workspace")


def test_lease_host_409_raises_host_name_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "name in use"})

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudHostNameConflictError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", host_name="my-workspace")


def test_lease_host_success_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["attributes"] == {"cpus": 2}
        assert body["ssh_public_key"] == "ssh-ed25519 AAAA"
        assert body["host_name"] == "my-workspace"
        return httpx.Response(
            200,
            json={
                "host_db_id": "00000000-0000-0000-0000-000000000001",
                "vps_ip": "10.0.0.1",
                "ssh_port": 22,
                "ssh_user": "root",
                "container_ssh_port": 2222,
                "agent_id": "agent-abc",
                "host_id": "host-xyz",
                "host_name": "my-workspace",
                "attributes": {"cpus": 2},
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    result = client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=2), "ssh-ed25519 AAAA", host_name="my-workspace")
    assert result.vps_ip == "10.0.0.1"
    assert result.agent_id == "agent-abc"
    assert result.host_name == "my-workspace"
    assert result.attributes == {"cpus": 2}


def test_rename_host_calls_patch_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client issues a PATCH /hosts/{id}/name with the new name in the body."""
    captured_url: list[str] = []
    captured_body: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url.append(str(request.url))
        captured_body.append(_json.loads(request.content))
        return httpx.Response(200, json={"status": "renamed", "host_name": "new-name"})

    transport = httpx.MockTransport(handler)

    def fake_patch(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.patch(*args, **kwargs)

    monkeypatch.setattr(httpx, "patch", fake_patch)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    client.rename_host(SecretStr("tok"), host_db_id="lease-1", new_host_name="new-name")
    assert captured_url[0].endswith("/hosts/lease-1/name")
    assert captured_body[0] == {"host_name": "new-name"}


def test_rename_host_409_raises_host_name_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "name in use"})

    transport = httpx.MockTransport(handler)

    def fake_patch(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.patch(*args, **kwargs)

    monkeypatch.setattr(httpx, "patch", fake_patch)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudHostNameConflictError):
        client.rename_host(SecretStr("tok"), host_db_id="lease-1", new_host_name="taken")


def test_unauthenticated_responses_raise_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "no token"})

    transport = httpx.MockTransport(handler)

    def fake_get(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.get(*args, **kwargs)

    monkeypatch.setattr(httpx, "get", fake_get)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudAuthError):
        client.list_hosts(SecretStr("tok"))


def test_500_lease_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    def fake_post(*args, **kwargs):
        with httpx.Client(transport=transport) as inner:
            return inner.post(*args, **kwargs)

    monkeypatch.setattr(httpx, "post", fake_post)
    client = ImbueCloudConnectorClient(base_url=AnyUrl("https://example.com"))
    with pytest.raises(ImbueCloudConnectorError):
        client.lease_host(SecretStr("tok"), LeaseAttributes(cpus=1), "ssh-ed25519 X", host_name="x")


# -- AuthPolicy translation --
#
# The connector's API takes/returns the Cloudflare-native ``{"rules": [...]}``
# shape; the plugin's ``AuthPolicy`` is the high-level ``emails / email_domains
# / require_idp`` shape. The client translates at every wire boundary so the
# plugin CLI's user-facing surface stays high-level. These tests pin the
# translation -- before they existed, the bug went unnoticed and ``set
# service auth`` failed at runtime with a 422 from the connector.


def test_auth_policy_to_connector_body_translates_emails_domains_idps() -> None:
    body = _auth_policy_to_connector_body(
        AuthPolicy(
            emails=("a@b.com", "c@d.com"),
            email_domains=("e.com",),
            require_idp=("idp1",),
        )
    )
    assert body == {
        "rules": [
            {
                "action": "allow",
                "include": [
                    {"email": {"email": "a@b.com"}},
                    {"email": {"email": "c@d.com"}},
                    {"email_domain": {"domain": "e.com"}},
                    {"login_method": {"id": "idp1"}},
                ],
            }
        ]
    }


def test_auth_policy_to_connector_body_emits_empty_rules_for_empty_policy() -> None:
    """An empty policy must serialize to ``{"rules": []}`` rather than a rule with an empty include."""
    assert _auth_policy_to_connector_body(AuthPolicy()) == {"rules": []}


def test_parse_auth_policy_round_trips_emails_domains_idps() -> None:
    original = AuthPolicy(
        emails=("a@b.com", "c@d.com"),
        email_domains=("e.com",),
        require_idp=("idp1",),
    )
    assert _parse_auth_policy(_auth_policy_to_connector_body(original)) == original


def test_parse_auth_policy_handles_empty_response() -> None:
    """``GET ... /auth`` returns ``{"rules": []}`` when no policy is configured."""
    assert _parse_auth_policy({"rules": []}) == AuthPolicy()


def test_parse_auth_policy_ignores_unknown_include_types() -> None:
    """A future Cloudflare include shape (e.g. ``{"github": ...}``) must not break older clients."""
    parsed = _parse_auth_policy(
        {
            "rules": [
                {
                    "action": "allow",
                    "include": [
                        {"email": {"email": "a@b.com"}},
                        {"github": {"team": "secret"}},
                    ],
                }
            ]
        }
    )
    assert parsed == AuthPolicy(emails=("a@b.com",))
